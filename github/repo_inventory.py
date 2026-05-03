#!/usr/bin/env python3
"""
GitHub Repository Inventory Generator

A local-only tool that inventories GitHub repositories and outputs a CSV
with detailed metadata, classifications, and risk signals.
"""

import argparse
import csv
import json
import logging
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock, Semaphore
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import quote

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class GitHubAPI:
    """Wrapper for GitHub API calls via gh CLI."""
    
    def __init__(self, timeout: int = 30, max_retries: int = 3, api_concurrency: int = 10, rate_limit_delay: float = 0.1):
        self.timeout = timeout
        self.max_retries = max_retries
        self._cache: Dict[str, any] = {}
        self._cache_lock = Lock()
        self._rate_limit_semaphore = Semaphore(api_concurrency)
        self._rate_limit_delay = rate_limit_delay
        self._last_api_call_time = 0
        self._rate_limit_lock = Lock()
    
    def _run_gh_api(self, endpoint: str, retry_count: int = 0) -> Optional[Dict]:
        """Run gh api command with retry logic and rate limiting."""
        # Check cache
        cache_key = endpoint
        with self._cache_lock:
            if cache_key in self._cache:
                return self._cache[cache_key]
        
        # Rate limiting: acquire semaphore and add delay
        with self._rate_limit_semaphore:
            with self._rate_limit_lock:
                # Add small delay between API calls to avoid hitting rate limits
                current_time = time.time()
                time_since_last_call = current_time - self._last_api_call_time
                if time_since_last_call < self._rate_limit_delay:
                    time.sleep(self._rate_limit_delay - time_since_last_call)
                self._last_api_call_time = time.time()
        
        try:
            cmd = ['gh', 'api', endpoint]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=True
            )
            
            # Check for rate limit headers in stderr (gh CLI may output warnings there)
            if result.stderr:
                if 'rate limit' in result.stderr.lower() or '429' in result.stderr:
                    logger.warning(f"Rate limit detected, waiting before retry: {endpoint}")
                    if retry_count < self.max_retries:
                        # Wait longer for rate limits (exponential backoff)
                        delay = min(60 * (2 ** retry_count), 300)  # Max 5 minutes
                        logger.info(f"Waiting {delay}s for rate limit to reset...")
                        time.sleep(delay)
                        return self._run_gh_api(endpoint, retry_count + 1)
            
            data = json.loads(result.stdout)
            with self._cache_lock:
                self._cache[cache_key] = data
            return data
        except subprocess.TimeoutExpired:
            logger.warning(f"Timeout calling {endpoint}")
            return None
        except subprocess.CalledProcessError as e:
            # Check for rate limit errors (429)
            if e.returncode == 429 or (e.stderr and ('rate limit' in e.stderr.lower() or '429' in e.stderr)):
                if retry_count < self.max_retries:
                    delay = min(60 * (2 ** retry_count), 300)  # Exponential backoff, max 5 minutes
                    logger.warning(f"Rate limit hit (429), waiting {delay}s before retry: {endpoint}")
                    time.sleep(delay)
                    return self._run_gh_api(endpoint, retry_count + 1)
                logger.error(f"Rate limit exceeded after {self.max_retries} retries: {endpoint}")
                return None
            if e.returncode == 404:
                return None
            if retry_count < self.max_retries:
                delay = 2 ** retry_count
                logger.warning(f"API call failed, retrying in {delay}s: {endpoint}")
                time.sleep(delay)
                return self._run_gh_api(endpoint, retry_count + 1)
            logger.error(f"API call failed after {self.max_retries} retries: {endpoint}")
            return None
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON response from {endpoint}")
            return None
    
    def list_repos(self, org: str, include_archived: bool = False, include_forks: bool = False) -> List[Dict]:
        """List all repositories in an organization."""
        repos = []
        page = 1
        per_page = 100
        
        while True:
            endpoint = f"/orgs/{org}/repos?per_page={per_page}&page={page}"
            data = self._run_gh_api(endpoint)
            
            if not data or not isinstance(data, list):
                break
            
            for repo in data:
                if not include_archived and repo.get('archived', False):
                    continue
                if not include_forks and repo.get('fork', False):
                    continue
                repos.append(repo)
            
            if len(data) < per_page:
                break
            page += 1
        
        return repos
    
    def get_repo_languages(self, org: str, repo: str) -> Dict[str, int]:
        """Get repository languages."""
        endpoint = f"/repos/{org}/{repo}/languages"
        return self._run_gh_api(endpoint) or {}
    
    def get_repo_releases(self, org: str, repo: str, exclude_prereleases: bool = False) -> List[Dict]:
        """Get repository releases."""
        endpoint = f"/repos/{org}/{repo}/releases"
        releases = self._run_gh_api(endpoint) or []
        
        if exclude_prereleases:
            releases = [r for r in releases if not r.get('prerelease', False)]
        
        return releases
    
    def get_repo_tags(self, org: str, repo: str) -> List[Dict]:
        """Get repository tags."""
        endpoint = f"/repos/{org}/{repo}/tags"
        return self._run_gh_api(endpoint) or []
    
    def get_repo_branches(self, org: str, repo: str) -> List[Dict]:
        """Get repository branches."""
        branches = []
        page = 1
        per_page = 100
        
        while True:
            endpoint = f"/repos/{org}/{repo}/branches?per_page={per_page}&page={page}"
            data = self._run_gh_api(endpoint)
            
            if not data or not isinstance(data, list):
                break
            
            branches.extend(data)
            
            if len(data) < per_page:
                break
            page += 1
        
        return branches
    
    def get_latest_commit(self, org: str, repo: str, branch: str) -> Optional[Dict]:
        """Get latest commit on a branch."""
        endpoint = f"/repos/{org}/{repo}/commits/{branch}"
        commit = self._run_gh_api(endpoint)
        if isinstance(commit, dict):
            return commit
        return None


class GitOperations:
    """Wrapper for git operations."""
    
    @staticmethod
    def clone_repo(url: str, dest: Path, mode: str = 'blobless', skip_existing: bool = False) -> bool:
        """Clone a repository."""
        if skip_existing and dest.exists():
            return True
        
        dest.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            if mode == 'none':
                return False
            elif mode == 'blobless':
                subprocess.run(
                    ['git', 'clone', '--filter=blob:none', '--depth', '1', url, str(dest)],
                    capture_output=True,
                    check=True,
                    timeout=300
                )
            elif mode == 'full':
                subprocess.run(
                    ['git', 'clone', url, str(dest)],
                    capture_output=True,
                    check=True,
                    timeout=600
                )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.error(f"Failed to clone {url}: {e}")
            return False
    
    @staticmethod
    def list_files(repo_path: Path, include_submodules: bool = False) -> List[str]:
        """List all files in repository."""
        if not repo_path.exists():
            return []
        
        try:
            result = subprocess.run(
                ['git', 'ls-tree', '-r', '--name-only', 'HEAD'],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=True,
                timeout=60
            )
            files = result.stdout.strip().split('\n') if result.stdout.strip() else []
            
            # Filter out .git directory
            files = [f for f in files if not f.startswith('.git/')]
            
            # Filter submodules if needed
            if not include_submodules:
                files = [f for f in files if not f.endswith('.git')]
            
            return files
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return []
    
    @staticmethod
    def read_file(repo_path: Path, file_path: str, max_bytes: int = 10240) -> Optional[str]:
        """Read a file from repository."""
        full_path = repo_path / file_path
        if not full_path.exists():
            return None
        
        try:
            with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read(max_bytes)
            return content
        except Exception:
            return None


class PatternMatcher:
    """Match file patterns for classification."""
    
    IAC_PATTERNS = [
        r'\.tf$', r'\.tfvars$', r'\.tflint\.hcl$', r'terraform\.lock\.hcl$', r'\.tf\.json$',
        r'terragrunt\.hcl$',
        r'template\.ya?ml$', r'\.cfn\.ya?ml$', r'cloudformation/',
        r'cdk\.json$', r'cdk\.context\.json$',
        r'Pulumi\.ya?ml$', r'pulumi\.',
        r'Chart\.ya?ml$', r'charts/', r'values\.ya?ml$',
        r'kustomization\.ya?ml$',
        r'ansible/', r'playbook\.ya?ml$', r'ansible\.cfg$',
        r'packer\.hcl$', r'packer\.json$',
        r'\.tofu$', r'tofu\.lock\.hcl$'
    ]
    
    CODE_PATTERNS = [
        r'\.rb$', r'Gemfile', r'config/application\.rb$', r'Rakefile$',
        r'\.py$', r'pyproject\.toml$', r'setup\.py$', r'setup\.cfg$', r'Pipfile$',
        r'\.go$', r'go\.mod$', r'go\.sum$',
        r'\.tsx?$', r'\.jsx?$', r'package\.json$',
        r'\.java$', r'\.kt$', r'\.scala$', r'pom\.xml$', r'build\.gradle$', r'build\.gradle\.kts$',
        r'Dockerfile', r'docker-compose\.ya?ml$', r'\.dockerignore$',
        r'\.rs$', r'Cargo\.toml$',
        r'\.c$', r'\.cpp$', r'\.cc$', r'\.h$', r'\.hpp$', r'CMakeLists\.txt$', r'Makefile$',
        r'\.cs$', r'\.csproj$'
    ]
    
    PIPELINE_PATTERNS = [
        r'\.github/workflows/.*\.ya?ml$',
        r'Jenkinsfile',
        r'\.gitlab-ci\.ya?ml$',
        r'\.buildkite/', r'buildkite\.ya?ml$',
        r'\.circleci/config\.ya?ml$',
        r'azure-pipelines\.ya?ml$',
        r'\.travis\.ya?ml$',
        r'\.drone\.ya?ml$'
    ]
    
    DOC_PATTERNS = [
        r'README', r'docs/', r'\.md$', r'\.rst$', r'\.txt$'
    ]
    
    @classmethod
    def match_patterns(cls, file_path: str, patterns: List[str]) -> bool:
        """Check if file path matches any pattern."""
        file_path_lower = file_path.lower()
        for pattern in patterns:
            if re.search(pattern, file_path_lower, re.IGNORECASE):
                return True
        return False
    
    @classmethod
    def count_hits(cls, files: List[str], pattern_type: str) -> int:
        """Count files matching pattern type."""
        if pattern_type == 'iac':
            patterns = cls.IAC_PATTERNS
        elif pattern_type == 'code':
            patterns = cls.CODE_PATTERNS
        elif pattern_type == 'pipeline':
            patterns = cls.PIPELINE_PATTERNS
        elif pattern_type == 'doc':
            patterns = cls.DOC_PATTERNS
        else:
            return 0
        
        return sum(1 for f in files if cls.match_patterns(f, patterns))


class RepoClassifier:
    """Classify repositories into categories and component types."""
    
    @staticmethod
    def classify_category(
        total_files: int,
        iac_hits: int,
        code_hits: int,
        pipeline_hits: int,
        doc_hits: int
    ) -> str:
        """Determine repository category."""
        if total_files == 0:
            return 'empty'
        
        iac_ratio = iac_hits / total_files if total_files > 0 else 0
        code_ratio = code_hits / total_files if total_files > 0 else 0
        doc_ratio = doc_hits / total_files if total_files > 0 else 0
        pipeline_ratio = pipeline_hits / total_files if total_files > 0 else 0
        
        if doc_ratio > 0.80 and iac_hits == 0 and code_hits == 0:
            return 'docs'
        if iac_ratio > 0.60 and code_ratio < 0.15:
            return 'iac'
        if code_ratio > 0.60 and iac_ratio < 0.10:
            return 'code'
        if pipeline_ratio > 0.25 and iac_ratio < 0.20 and code_ratio < 0.20:
            return 'pipelines'
        if iac_hits > 0 and code_hits > 0:
            return 'hybrid'
        
        return 'misc'
    
    @staticmethod
    def classify_component_type(
        repo_name: str,
        description: str,
        category: str,
        has_package: bool,
        total_files: int,
        iac_hits: int,
        code_hits: int,
        main_language: str,
        languages: Dict[str, int],
        files: List[str]
    ) -> str:
        """Determine component type."""
        repo_lower = repo_name.lower()
        desc_lower = (description or '').lower()
        
        if total_files == 0:
            return 'misc'
        
        iac_ratio = iac_hits / total_files if total_files > 0 else 0
        code_ratio = code_hits / total_files if total_files > 0 else 0
        
        # 1. iac
        if category == 'iac' or iac_ratio > 0.70:
            return 'iac'
        iac_keywords = ['terraform', 'terragrunt', 'pulumi', 'cdk', 'infra', 'infrastructure', 'iac', 'cloudformation', 'helm', 'kustomize']
        if any(kw in repo_lower for kw in iac_keywords):
            return 'iac'
        
        # 2. lambda
        lambda_keywords = ['lambda', 'function', 'serverless', 'faas']
        if any(kw in repo_lower or kw in desc_lower for kw in lambda_keywords):
            return 'lambda'
        lambda_files = ['serverless.yml', 'serverless.yaml', 'sam.yaml', 'template.yaml', 'function.json', 'handler.py', 'handler.js', 'index.js']
        if any(f in files for f in lambda_files) and total_files < 50:
            return 'lambda'
        
        # 3. library
        if has_package:
            lib_keywords = ['lib', 'library', 'sdk', 'client', 'package', 'module', 'utils', 'util']
            if any(kw in repo_lower or kw in desc_lower for kw in lib_keywords):
                if total_files < 500 or code_ratio > 0.80:
                    return 'library'
            lib_files = ['setup.py', 'pyproject.toml', 'Cargo.toml']
            if any(f in files for f in lib_files):
                if total_files < 500 or code_ratio > 0.80:
                    return 'library'
        
        # 4. ui
        ui_languages = ['typescript', 'javascript', 'css', 'scss', 'html', 'vue', 'react', 'angular']
        if main_language.lower() in ui_languages:
            return 'ui'
        ui_keywords = ['ui', 'frontend', 'web', 'app', 'client', 'dashboard', 'portal', 'interface']
        if any(kw in repo_lower for kw in ui_keywords):
            return 'ui'
        ui_files = ['vite.config', 'next.config', 'nuxt.config', 'angular.json', 'webpack.config']
        if any(any(uf in f for f in files) for uf in ui_files):
            return 'ui'
        if code_ratio > 0.60 and main_language.lower() in ['typescript', 'javascript'] and iac_hits == 0:
            return 'ui'
        
        # 5. db
        db_keywords = ['db', 'database', 'migration', 'schema', 'sql', 'postgres', 'mysql', 'mongo']
        if any(kw in repo_lower or kw in desc_lower for kw in db_keywords):
            return 'db'
        db_files = ['.sql', 'migrations/', 'schema/', 'seeds/', '.prisma', 'schema.prisma']
        if any(any(df in f for f in files) for df in db_files):
            return 'db'
        
        # 6. scripts
        if total_files < 100:
            script_languages = ['shell', 'python', 'ruby', 'perl']
            if code_ratio > 0.50 or main_language.lower() in script_languages:
                if iac_hits == 0 and pipeline_hits == 0:
                    script_keywords = ['script', 'tool', 'util', 'helper', 'bin']
                    if any(kw in repo_lower for kw in script_keywords) or any(f.endswith(('.sh', '.py')) for f in files if '/' not in f or f.count('/') == 0):
                        return 'scripts'
        
        # 7. micro-service
        if has_package and 50 <= total_files < 2000:
            if code_ratio > 0.40 or main_language.lower() in ['go', 'python', 'java', 'ruby', 'node.js']:
                service_indicators = ['Dockerfile', 'docker-compose', 'kubernetes/', 'k8s/', 'deployment.yaml', 'service.yaml']
                if any(any(si in f for f in files) for si in service_indicators):
                    return 'micro-service'
                service_keywords = ['service', 'api', 'microservice', 'ms-', 'svc-']
                if any(kw in repo_lower or kw in desc_lower for kw in service_keywords):
                    return 'micro-service'
        
        # 8. service
        if has_package and 2000 <= total_files < 10000:
            if code_ratio > 0.40:
                service_indicators = ['Dockerfile', 'kubernetes/', 'deployment.yaml', 'docker-compose']
                if any(any(si in f for f in files) for si in service_indicators):
                    return 'service'
                service_keywords = ['service', 'api', 'backend', 'server']
                if any(kw in repo_lower or kw in desc_lower for kw in service_keywords):
                    return 'service'
        
        # 9. monolith
        if total_files >= 10000:
            return 'monolith'
        if code_ratio > 0.50 and total_files >= 5000:
            if len([l for l, b in languages.items() if b > 0]) >= 3:
                return 'monolith'
            monolith_keywords = ['monolith', 'platform', 'app', 'application']
            if any(kw in repo_lower or kw in desc_lower for kw in monolith_keywords) and 'micro' not in repo_lower:
                return 'monolith'
        
        return 'misc'


class RiskSignalDetector:
    """Detect risk signals in repositories."""
    
    @staticmethod
    def detect_risk_signals(
        has_readme: bool,
        has_codeowners: bool,
        pipeline_hits: int,
        has_tags: bool,
        has_releases: bool,
        has_license: bool,
        iac_hits: int,
        files: List[str],
        total_files: int,
        num_languages: int,
        last_updated_at: str,
        is_archived: bool,
        main_branch: str,
        has_package: bool,
        stale_days: int = 365,
        large_files_threshold: int = 5000,
        many_languages_threshold: int = 5,
        hybrid_complexity_threshold: int = 2000,
        category: str = ''
    ) -> List[str]:
        """Detect risk signals."""
        signals = []
        
        if not has_readme:
            signals.append('no_readme')
        if not has_codeowners:
            signals.append('no_codeowners')
        if pipeline_hits == 0:
            signals.append('no_ci')
        if not has_tags:
            signals.append('no_tags')
        if not has_releases:
            signals.append('no_releases')
        if not has_license:
            signals.append('no_license')
        
        # iac_no_lock
        if iac_hits > 0:
            has_tf = any('.tf' in f for f in files)
            has_lock = any('terraform.lock.hcl' in f for f in files)
            if has_tf and not has_lock:
                signals.append('iac_no_lock')
        
        # secrets_suspected
        secret_patterns = ['.env', '.pem', 'id_rsa', '.p12', '.key', '.secret', 'credentials', 'secrets.', 'config/secrets.yml', '.pfx']
        if any(any(sp in f.lower() for f in files) for sp in secret_patterns):
            signals.append('secrets_suspected')
        
        # many_languages
        if num_languages >= many_languages_threshold:
            signals.append('many_languages')
        
        # large_repo
        if total_files > large_files_threshold:
            signals.append('large_repo')
        
        # stale_repo
        if last_updated_at:
            try:
                updated = datetime.fromisoformat(last_updated_at.replace('Z', '+00:00'))
                if (datetime.now(updated.tzinfo) - updated).days > stale_days:
                    signals.append('stale_repo')
            except Exception:
                pass
        
        # hybrid_complexity
        if category == 'hybrid' and total_files > hybrid_complexity_threshold:
            signals.append('hybrid_complexity')
        
        # archived_repo
        if is_archived:
            signals.append('archived_repo')
        
        # default_branch_not_main
        if main_branch.lower() not in ['main', 'master']:
            signals.append('default_branch_not_main')
        
        # has_dependencies_no_lock
        if has_package:
            dep_files = ['package.json', 'requirements.txt', 'Gemfile', 'go.mod', 'Cargo.toml']
            lock_files = ['package-lock.json', 'requirements.lock', 'Gemfile.lock', 'go.sum', 'Cargo.lock']
            has_dep = any(any(df in f for f in files) for df in dep_files)
            has_lock = any(any(lf in f for f in files) for lf in lock_files)
            if has_dep and not has_lock:
                signals.append('has_dependencies_no_lock')
        
        return sorted(signals)


class RepoProcessor:
    """Process a single repository."""
    
    def __init__(
        self,
        api: GitHubAPI,
        git_ops: GitOperations,
        clone_mode: str,
        clone_root: Path,
        skip_existing: bool,
        exclude_prereleases: bool,
        prefer_ssh: bool,
        include_submodules: bool,
        stale_days: int,
        large_files_threshold: int,
        many_languages_threshold: int,
        hybrid_complexity_threshold: int
    ):
        self.api = api
        self.git_ops = git_ops
        self.clone_mode = clone_mode
        self.clone_root = clone_root
        self.skip_existing = skip_existing
        self.exclude_prereleases = exclude_prereleases
        self.prefer_ssh = prefer_ssh
        self.include_submodules = include_submodules
        self.stale_days = stale_days
        self.large_files_threshold = large_files_threshold
        self.many_languages_threshold = many_languages_threshold
        self.hybrid_complexity_threshold = hybrid_complexity_threshold
    
    def process_repo(self, org: str, repo_data: Dict) -> Dict:
        """Process a single repository and return inventory data."""
        repo_name = repo_data['name']
        logger.info(f"Processing {repo_name}")
        
        result = {
            'repo_name': repo_name,
            'description': repo_data.get('description') or '',
            'latest_tag': '',
            'has_package?': 'false',
            'latest_release': '',
            'clone_url': '',
            'lanjuages_list': '',
            'main_language': '',
            'num_branches': 0,
            'main_branch': '',
            'category': 'unknown',
            'component_type': 'misc',
            'total_files': 0,
            'iac_hits': 0,
            'code_hits': 0,
            'pipeline_hits': 0,
            'doc_hits': 0,
            'has_readme?': 'false',
            'created_at': repo_data.get('created_at', ''),
            'last_updated_at': repo_data.get('pushed_at') or repo_data.get('updated_at', ''),
            'last_updated_by': '',
            'codeowners': 'present:false',
            'risk_signals': ''
        }
        
        try:
            # Get languages
            languages = self.api.get_repo_languages(org, repo_name) or {}
            if languages:
                lang_list = ';'.join(f"{k}:{v}" for k, v in languages.items())
                result['lanjuages_list'] = lang_list
                if languages:
                    result['main_language'] = max(languages.items(), key=lambda x: x[1])[0]
            
            # Get branches
            branches = self.api.get_repo_branches(org, repo_name) or []
            result['num_branches'] = len(branches)
            result['main_branch'] = (repo_data.get('default_branch') or 'main').lower()
            
            # Get tags and releases
            tags = self.api.get_repo_tags(org, repo_name) or []
            releases = self.api.get_repo_releases(org, repo_name, self.exclude_prereleases) or []
            
            if tags:
                # Sort tags by semantic version
                tag_names = [t.get('name', '') for t in tags if t.get('name')]
                if tag_names:
                    sorted_tags = self._sort_tags(tag_names)
                    result['latest_tag'] = sorted_tags[0] if sorted_tags else ''
            
            if releases:
                result['latest_release'] = releases[0].get('name') or releases[0].get('tag_name', '')
            
            # Clone URL
            if self.prefer_ssh:
                result['clone_url'] = repo_data.get('ssh_url', repo_data.get('clone_url', ''))
            else:
                result['clone_url'] = repo_data.get('clone_url', '')
            
            # Get latest commit author
            latest_commit = self.api.get_latest_commit(org, repo_name, result['main_branch'])
            if latest_commit:
                author = latest_commit.get('author')
                if author:
                    result['last_updated_by'] = author.get('login', '')
                    if not result['last_updated_by']:
                        commit_author = latest_commit.get('commit', {}).get('author', {})
                        name = commit_author.get('name', '')
                        email = commit_author.get('email', '')
                        if name or email:
                            result['last_updated_by'] = f"{name} <{email}>" if name and email else name or email
            
            # Clone and scan if needed
            files = []
            repo_path = None
            
            if self.clone_mode != 'none':
                clone_url = result['clone_url']
                repo_path = self.clone_root / repo_name
                
                if self.git_ops.clone_repo(clone_url, repo_path, self.clone_mode, self.skip_existing):
                    files = self.git_ops.list_files(repo_path, self.include_submodules)
                    result['total_files'] = len(files)
            
            # Pattern matching
            result['iac_hits'] = PatternMatcher.count_hits(files, 'iac')
            result['code_hits'] = PatternMatcher.count_hits(files, 'code')
            result['pipeline_hits'] = PatternMatcher.count_hits(files, 'pipeline')
            result['doc_hits'] = PatternMatcher.count_hits(files, 'doc')
            
            # Check for package manifests
            package_files = [
                'package.json', 'pnpm-lock.yaml', 'yarn.lock',
                'pyproject.toml', 'requirements.txt', 'setup.py', 'Pipfile',
                'Gemfile', '.ruby-version', 'Gemfile.lock',
                'go.mod', 'go.sum',
                'pom.xml', 'build.gradle', 'build.gradle.kts',
                'Cargo.toml', 'Cargo.lock'
            ]
            package_extensions = ['.csproj', '.fsproj', '.vbproj', '.sln']
            has_package = False
            for f in files:
                f_lower = f.lower()
                # Check exact matches
                if any(f_lower.endswith(pf.lower()) or f_lower == pf.lower() for pf in package_files):
                    has_package = True
                    break
                # Check extensions
                if any(f_lower.endswith(ext) for ext in package_extensions):
                    has_package = True
                    break
            result['has_package?'] = 'true' if has_package else 'false'
            
            # Check for README (root directory only)
            has_readme = False
            for f in files:
                f_lower = f.lower()
                # Check if file is in root (no / or only one / at start for .github/)
                if '/' not in f or (f.startswith('.github/') and f.count('/') == 1):
                    if any(f_lower == rf.lower() or f_lower.endswith('/' + rf.lower()) for rf in ['README.md', 'README.rst', 'README.txt', 'README', 'readme.md']):
                        has_readme = True
                        break
            result['has_readme?'] = 'true' if has_readme else 'false'
            
            # Check for LICENSE (root directory only)
            has_license = False
            for f in files:
                f_lower = f.lower()
                if '/' not in f:
                    if any(f_lower == lf.lower() or f_lower.startswith(lf.lower() + '.') for lf in ['LICENSE', 'LICENSE.txt', 'LICENSE.md']):
                        has_license = True
                        break
            
            # CODEOWNERS
            codeowners_files = ['CODEOWNERS', '.github/CODEOWNERS', 'docs/CODEOWNERS']
            codeowners_path = None
            for cof in codeowners_files:
                for f in files:
                    if f == cof or f.endswith('/' + cof):
                        if repo_path:
                            codeowners_path = repo_path / f
                        break
                if codeowners_path:
                    break
            
            if codeowners_path and codeowners_path.exists():
                owners = self._parse_codeowners(codeowners_path)
                if owners:
                    result['codeowners'] = f"present:true;owners={','.join(sorted(owners))}"
                else:
                    result['codeowners'] = 'present:true'
            else:
                result['codeowners'] = 'present:false'
            
            # Classify
            result['category'] = RepoClassifier.classify_category(
                result['total_files'],
                result['iac_hits'],
                result['code_hits'],
                result['pipeline_hits'],
                result['doc_hits']
            )
            
            result['component_type'] = RepoClassifier.classify_component_type(
                repo_name,
                result['description'],
                result['category'],
                has_package,
                result['total_files'],
                result['iac_hits'],
                result['code_hits'],
                result['main_language'],
                languages,
                files
            )
            
            # Risk signals
            risk_signals = RiskSignalDetector.detect_risk_signals(
                has_readme,
                result['codeowners'] != 'present:false',
                result['pipeline_hits'],
                bool(tags),
                bool(releases),
                has_license,
                result['iac_hits'],
                files,
                result['total_files'],
                len(languages),
                result['last_updated_at'],
                repo_data.get('archived', False),
                result['main_branch'],
                has_package,
                self.stale_days,
                self.large_files_threshold,
                self.many_languages_threshold,
                self.hybrid_complexity_threshold,
                result['category']
            )
            result['risk_signals'] = ';'.join(risk_signals)
            
        except Exception as e:
            logger.error(f"Error processing {repo_name}: {e}")
            result['risk_signals'] = f"error:{str(e)}"
        
        return result
    
    def _sort_tags(self, tags: List[str]) -> List[str]:
        """Sort tags by semantic version."""
        def version_key(tag):
            # Remove 'v' prefix
            tag = tag.lstrip('v')
            # Extract version parts
            parts = re.findall(r'\d+', tag)
            if parts:
                return tuple(int(p) for p in parts)
            return (0,)
        
        return sorted(tags, key=version_key, reverse=True)
    
    def _parse_codeowners(self, codeowners_path: Path) -> Set[str]:
        """Parse CODEOWNERS file and extract owners."""
        owners = set()
        try:
            with open(codeowners_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            for line in content.split('\n'):
                line = line.strip()
                if line and not line.startswith('#'):
                    # Extract @mentions
                    mentions = re.findall(r'@[\w/-]+', line)
                    owners.update(mentions)
        except Exception:
            pass
        return owners


def validate_prerequisites() -> Tuple[bool, str]:
    """Validate prerequisites before starting."""
    # Check gh CLI
    try:
        subprocess.run(['which', 'gh'], capture_output=True, check=True)
    except subprocess.CalledProcessError:
        return False, "GitHub CLI (gh) is not installed"
    
    # Check gh auth
    try:
        result = subprocess.run(['gh', 'auth', 'status'], capture_output=True, text=True, check=True)
        if 'Logged in' not in result.stdout:
            return False, "GitHub CLI is not authenticated. Run 'gh auth login'"
    except subprocess.CalledProcessError:
        return False, "GitHub CLI authentication failed. Run 'gh auth login'"
    
    # Check disk space (simplified check)
    try:
        stat = os.statvfs('.')
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
        if free_gb < 1:
            return False, f"Low disk space: {free_gb:.2f}GB free (need at least 1GB)"
    except Exception:
        pass  # Skip disk check if it fails
    
    return True, ""


def load_config(config_path: Path) -> Dict:
    """Load configuration from JSON or YAML file."""
    if not config_path.exists():
        return {}
    
    try:
        if config_path.suffix == '.yaml' or config_path.suffix == '.yml':
            import yaml
            with open(config_path, 'r') as f:
                return yaml.safe_load(f) or {}
        else:
            with open(config_path, 'r') as f:
                return json.load(f) or {}
    except Exception as e:
        logger.warning(f"Failed to load config: {e}")
        return {}


def write_csv(results: List[Dict], output_path: Path):
    """Write results to CSV file."""
    headers = [
        'repo_name', 'description', 'latest_tag', 'has_package?', 'latest_release',
        'clone_url', 'lanjuages_list', 'main_language', 'num_branches', 'main_branch',
        'category', 'component_type', 'total_files', 'iac_hits', 'code_hits',
        'pipeline_hits', 'doc_hits', 'has_readme?', 'created_at', 'last_updated_at',
        'last_updated_by', 'codeowners', 'risk_signals'
    ]
    
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for result in results:
            writer.writerow(result)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Generate inventory of GitHub repositories',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument('--org', required=True, help='GitHub organization name')
    parser.add_argument('--output', default='repo_inventory.csv', help='Output CSV file path')
    parser.add_argument('--clone-mode', choices=['none', 'blobless', 'full'], default='blobless',
                       help='Cloning strategy')
    parser.add_argument('--clone-root', default='./repos', help='Root directory for cloned repositories')
    parser.add_argument('--include-archived', action='store_true', help='Include archived repositories')
    parser.add_argument('--include-forks', action='store_true', help='Include forked repositories')
    parser.add_argument('--max-repos', type=int, help='Maximum number of repos to analyze (processes first N repos after filtering)')
    parser.add_argument('--repo-name-starts-with', help='Filter repos whose name starts with this value (case-insensitive)')
    parser.add_argument('--repo-name-contains', help='Filter repos whose name contains this value (case-insensitive)')
    parser.add_argument('--repo-name-pattern', help='Filter repos whose name matches this regex pattern')
    parser.add_argument('--concurrency', type=int, default=8, help='Number of concurrent repo processing threads')
    parser.add_argument('--api-concurrency', type=int, default=10, help='Number of concurrent API calls')
    parser.add_argument('--rate-limit-delay', type=float, default=0.1, help='Delay in seconds between API calls to avoid rate limits')
    parser.add_argument('--stale-days', type=int, default=365, help='Days threshold for stale repo detection')
    parser.add_argument('--large-files-threshold', type=int, default=5000,
                       help='File count threshold for large repo detection')
    parser.add_argument('--many-languages-threshold', type=int, default=5,
                       help='Language count threshold for many_languages flag')
    parser.add_argument('--hybrid-complexity-threshold', type=int, default=2000,
                       help='File count threshold for hybrid_complexity flag')
    parser.add_argument('--skip-large-repos', type=int, default=100,
                       help='Skip repos larger than this size in MB (full clone mode only)')
    parser.add_argument('--api-timeout', type=int, default=30, help='Timeout in seconds for API calls')
    parser.add_argument('--exclude-prereleases', action='store_true',
                       help='Exclude pre-releases from latest_tag/latest_release')
    parser.add_argument('--prefer-ssh', action='store_true', help='Prefer SSH URLs over HTTPS for clone_url')
    parser.add_argument('--include-submodules', action='store_true', help='Include git submodules in file counts')
    parser.add_argument('--exclude-binary', action='store_true', help='Exclude binary files from total_files count')
    parser.add_argument('--cleanup', action='store_true', help='Remove cloned repositories after scanning')
    parser.add_argument('--skip-existing', action='store_true',
                       help='Skip repos that already exist in clone-root (faster re-runs)')
    parser.add_argument('--resume', action='store_true', help='Resume from last processed repo (requires --log-jsonl)')
    parser.add_argument('--log-jsonl', help='Path to JSONL log file for detailed debugging')
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable verbose logging')
    parser.add_argument('--dry-run', action='store_true',
                       help='Test API access and validate configuration without cloning')
    parser.add_argument('--config', help='Path to configuration file (JSON or YAML) for repeated runs')
    parser.add_argument('--version', action='version', version='1.0.0')
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Load config file if provided
    config = {}
    if args.config:
        config = load_config(Path(args.config))
        # Override with command-line arguments
        for key, value in vars(args).items():
            if value is not None and key != 'config':
                config[key.replace('-', '_')] = value
        args = argparse.Namespace(**config)
    
    # Validate prerequisites
    if not args.dry_run:
        valid, msg = validate_prerequisites()
        if not valid:
            logger.error(msg)
            sys.exit(2)
    
    # Initialize components
    api = GitHubAPI(
        timeout=args.api_timeout,
        api_concurrency=args.api_concurrency,
        rate_limit_delay=getattr(args, 'rate_limit_delay', 0.1)
    )
    git_ops = GitOperations()
    clone_root = Path(args.clone_root)
    clone_root.mkdir(parents=True, exist_ok=True)
    
    processor = RepoProcessor(
        api, git_ops, args.clone_mode, clone_root, args.skip_existing,
        args.exclude_prereleases, args.prefer_ssh, args.include_submodules,
        args.stale_days, args.large_files_threshold, args.many_languages_threshold,
        args.hybrid_complexity_threshold
    )
    
    # List repositories
    logger.info(f"Fetching repositories for organization: {args.org}")
    repos = api.list_repos(args.org, args.include_archived, args.include_forks)
    
    # Apply repo name filters
    original_count = len(repos)
    if args.repo_name_starts_with:
        pattern = args.repo_name_starts_with.lower()
        repos = [r for r in repos if r['name'].lower().startswith(pattern)]
        logger.info(f"Filtered to {len(repos)} repos starting with '{args.repo_name_starts_with}' (from {original_count})")
        original_count = len(repos)
    
    if args.repo_name_contains:
        pattern = args.repo_name_contains.lower()
        repos = [r for r in repos if pattern in r['name'].lower()]
        logger.info(f"Filtered to {len(repos)} repos containing '{args.repo_name_contains}' (from {original_count})")
        original_count = len(repos)
    
    if args.repo_name_pattern:
        try:
            pattern = re.compile(args.repo_name_pattern, re.IGNORECASE)
            repos = [r for r in repos if pattern.search(r['name'])]
            logger.info(f"Filtered to {len(repos)} repos matching pattern '{args.repo_name_pattern}' (from {original_count})")
            original_count = len(repos)
        except re.error as e:
            logger.error(f"Invalid regex pattern '{args.repo_name_pattern}': {e}")
            sys.exit(2)
    
    # Apply max-repos limit (after filtering)
    if args.max_repos:
        if len(repos) > args.max_repos:
            logger.info(f"Limiting to first {args.max_repos} repos (from {len(repos)} after filtering)")
            repos = repos[:args.max_repos]
    
    logger.info(f"Found {len(repos)} repositories to process")
    
    if args.dry_run:
        logger.info("Dry run mode: validating configuration only")
        sys.exit(0)
    
    # Process repositories
    results = []
    jsonl_file = None
    if args.log_jsonl:
        jsonl_file = open(args.log_jsonl, 'w')
    
    start_time = time.time()
    processed = 0
    
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {
            executor.submit(processor.process_repo, args.org, repo): repo
            for repo in repos
        }
        
        for future in as_completed(futures):
            repo = futures[future]
            try:
                result = future.result()
                results.append(result)
                processed += 1
                
                if jsonl_file:
                    jsonl_file.write(json.dumps(result) + '\n')
                    jsonl_file.flush()
                
                logger.info(f"Progress: {processed}/{len(repos)} ({processed*100//len(repos)}%)")
            except Exception as e:
                logger.error(f"Failed to process {repo['name']}: {e}")
                processed += 1
    
    if jsonl_file:
        jsonl_file.close()
    
    # Write CSV
    output_path = Path(args.output)
    write_csv(results, output_path)
    logger.info(f"Results written to {output_path}")
    
    # Print summary
    elapsed = time.time() - start_time
    logger.info(f"\nSummary:")
    logger.info(f"  Total repos processed: {len(results)}")
    logger.info(f"  Processing time: {elapsed:.2f}s")
    
    # Category breakdown
    categories = {}
    for r in results:
        cat = r.get('category', 'unknown')
        categories[cat] = categories.get(cat, 0) + 1
    logger.info(f"  Categories: {categories}")
    
    # Cleanup if requested
    if args.cleanup:
        logger.info("Cleaning up cloned repositories...")
        import shutil
        for repo in repos:
            repo_path = clone_root / repo['name']
            if repo_path.exists():
                shutil.rmtree(repo_path, ignore_errors=True)
    
    sys.exit(0)


if __name__ == '__main__':
    main()
