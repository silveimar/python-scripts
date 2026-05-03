## Spec: Local Repo Inventory Generator (Python)

### Goal

Build a **local-only** Python tool that inventories all Kompliant GitHub repositories and outputs a CSV with these columns:

`repo_name,description,latest_tag,has_package?,latest_release,clone_url,lanjuages_list,main_language,num_branches,main_branch,category,component_type,total_files,iac_hits,code_hits,pipeline_hits,doc_hits,has_readme?,created_at,last_updated_at,last_updated_by,codeowners,risk_signals`

> Note: keep the CSV header **exactly** as above, including the `?` characters and the misspelling `lanjuages_list`.

### Privacy / Constraints

* Runs **only on the user's laptop**.
* Uses **GitHub CLI auth** (`gh auth`) or a locally provided GitHub token; no external services.
* No repo content is sent anywhere. The tool only produces local CSV + optional local logs.

### Inputs

* GitHub org name (e.g., `kompliant` or whichever org contains the repos).
* Optional flags (see CLI / Usage Requirements section for complete list).

### Output

* `repo_inventory.csv` in the current directory (or `--output` path).
* Optional `repo_inventory.jsonl` debug log (one JSON object per repo) for troubleshooting.
* Optional summary statistics printed to stdout.

---

## Data Collection Strategy

### Prefer GitHub API via `gh` (simpler, respects auth)

Use `gh api` for:

* Repo list & metadata: `name`, `description`, `clone_url`, `created_at`, `updated_at`, `pushed_at`, `default_branch`, `archived`, `fork`, `visibility`
* Languages endpoint
* Releases endpoint
* Tags endpoint
* Branch list/count
* Latest commit on default branch (for `last_updated_by`)
* License detection (if available)

Do **not** require third-party GitHub SDKs unless necessary. `subprocess` calling `gh api` is acceptable.

**API Call Strategy:**

* Implement retry logic with exponential backoff (3 retries max, 1s/2s/4s delays).
* Set timeouts for all API calls (default 30s, configurable via `--api-timeout`).
* Cache repo metadata responses during a single run (repos don't change mid-execution).
* Validate `gh` is installed and authenticated before starting (check `gh auth status`).

### Repo scanning (for counts + classification)

Support 3 modes:

1. **none**: no cloning; set `total_files/iac_hits/...` to `0`; category derived from GitHub languages + repo name heuristics only.
2. **blobless (recommended default)**:

   * `git clone --filter=blob:none --depth 1`
   * use `git ls-tree -r --name-only HEAD` to list file paths **without blobs**
   * classification uses filenames/paths/extensions only
   * Exclude `.git/` directory from file counts
   * Handle symlinks (count once, don't follow)
   * Handle git submodules (count as 1 entry or exclude based on `--include-submodules` flag)
3. **full**:

   * full clone for deep scanning (optional)
   * allows content keyword scanning for risk signals (still local)
   * Warn if repo exceeds `--skip-large-repos` threshold (default: 100MB)

Blobless mode should be enough for:

* `total_files`
* `iac_hits/code_hits/pipeline_hits/doc_hits`
* `has_readme?`
* `codeowners` presence + owners extraction (requires reading CODEOWNERS file; blobless clone may still fetch needed blobs when reading a file—acceptable since it's local)
* License file detection
* `component_type` determination (uses file structure, naming patterns, and framework files)

**File Counting Rules:**

* Exclude `.git/` directory and all git metadata
* Count symlinks as single entries (don't follow)
* Optionally exclude binary files from counts (configurable via `--exclude-binary`)
* Stream file listings for very large repos to avoid memory issues

---

## Column Definitions / How to Populate

### repo_name

* Repo name from GitHub.
* Sanitize for filesystem use when creating local directories (prevent path traversal).

### description

* Repo description (empty string if null).
* Escape special characters for CSV (quotes, newlines, commas).

### latest_tag

* Most recent tag by semantic version sort (proper semver comparison: `v1.10.0` > `v1.9.0`).
* Sort tags using semantic versioning library or custom comparator.
* Filter pre-releases if `--exclude-prereleases` flag is set.
* If no tags: empty string.

### latest_release

* Latest GitHub release tag/name (from releases API).
* Filter pre-releases if `--exclude-prereleases` flag is set.
* If no release: empty string.
* Prefer release name over tag name if both available.

### clone_url

* Use `sshUrl` or `clone_url` from GitHub; prefer SSH if environment expects it.
* Configurable via `--prefer-ssh` flag (default: detect from `gh` config).

### has_package?

Boolean (`true/false`), true if repo contains typical package manifests (by filename presence):

* Node: `package.json`, `pnpm-lock.yaml`, `yarn.lock`
* Python: `pyproject.toml`, `requirements.txt`, `setup.py`, `Pipfile`
* Ruby: `Gemfile`, `.ruby-version`, `Gemfile.lock`
* Go: `go.mod`, `go.sum`
* Java: `pom.xml`, `build.gradle`, `build.gradle.kts`
* Dotnet: `*.csproj`, `*.fsproj`, `*.vbproj`, `*.sln`
* Rust: `Cargo.toml`, `Cargo.lock`
* Also accept `Dockerfile` as *not* a "package" indicator by itself (optional), but include it under code signals.

### lanjuages_list

* From GitHub languages API: output as a semicolon-separated list like `Ruby:12000;HCL:4000;Shell:900`.
* Keep spelling exactly: `lanjuages_list`.
* If no languages detected: empty string.
* Handle edge case: repos with only binary files or no code.

### main_language

* The language with highest byte count from languages endpoint.
* If none: empty string.
* Consider confidence threshold: if top language is < 50% of total bytes, consider marking as "mixed" or empty.

### num_branches

* Count branches via API (paginated) OR `gh api repos/{org}/{repo}/branches --paginate | jq length`.
* Must be an integer.
* Handle pagination correctly (GitHub API returns max 100 per page).

### main_branch

* Default branch from repo metadata.
* Fallback detection: try `main`, `master`, `develop` in order if API fails.
* Normalize to lowercase for consistency.

### total_files

* If clone_mode != none: count of file paths from `git ls-tree -r --name-only HEAD`.
* Exclude `.git/` directory and git metadata.
* Handle symlinks (count once).
* Handle submodules (based on `--include-submodules` flag).
* Else: `0` (consistent with other numeric fields).

### iac_hits / code_hits / pipeline_hits / doc_hits

Counts of file paths matching patterns (regex or suffix/path rules). Patterns are case-insensitive unless specified.

**IaC signatures (iac_hits)**

* Terraform: `.tf`, `.tfvars`, `.tflint.hcl`, `terraform.lock.hcl`, `*.tf.json`
* Terragrunt: `terragrunt.hcl`
* CloudFormation: `template.yaml`, `template.yml`, `*.cfn.yaml`, `*.cfn.yml`, `cloudformation/`
* CDK: `cdk.json`, `cdk.context.json`
* Pulumi: `Pulumi.yaml`, `Pulumi.<stack>.yaml`, `pulumi.*`, `__main__.py` + `Pulumi.yaml`
* Helm: `Chart.yaml`, `charts/`, `values.yaml`
* Kustomize: `kustomization.yaml`, `kustomization.yml`
* Ansible: `ansible/`, `playbook.yml`, `playbook.yaml`, `ansible.cfg`
* Packer: `packer.hcl`, `packer.json`
* OpenTofu: `.tofu`, `tofu.lock.hcl` (if distinct from Terraform)

**Code signatures (code_hits)**

* Ruby/rails: `.rb`, `Gemfile`, `config/application.rb`, `Rakefile`
* Python: `.py`, `pyproject.toml`, `setup.py`, `setup.cfg`, `Pipfile`
* Go: `.go`, `go.mod`, `go.sum`
* TS/JS: `.ts`, `.tsx`, `.js`, `.jsx`, `package.json`
* Java: `.java`, `.kt`, `.scala`, `pom.xml`, `build.gradle`, `build.gradle.kts`
* Docker/build: `Dockerfile`, `docker-compose.yml`, `docker-compose.yaml`, `.dockerignore`
* Rust: `.rs`, `Cargo.toml`
* C/C++: `.c`, `.cpp`, `.cc`, `.h`, `.hpp`, `CMakeLists.txt`, `Makefile`
* C#: `.cs`, `*.csproj`

**Pipeline signatures (pipeline_hits)**

* GitHub Actions: `.github/workflows/*.yml`, `.github/workflows/*.yaml`
* Jenkins: `Jenkinsfile`, `Jenkinsfile.*`
* GitLab CI: `.gitlab-ci.yml`
* Buildkite: `.buildkite/`, `buildkite.yml`
* CircleCI: `.circleci/config.yml`
* Azure Pipelines: `azure-pipelines.yml`, `azure-pipelines.yaml`
* Travis CI: `.travis.yml`
* Drone CI: `.drone.yml`

**Docs signatures (doc_hits)**

* `README*` (case-insensitive), `docs/`, `.md`, `.rst`, `.txt` (in docs/), `*.md`, `*.rst`
* Exclude `README` files from code_hits if they're markdown.

### has_readme?

Boolean (`true/false`) if any of:

* `README.md`, `README.rst`, `README.txt`, `README`, `readme.md` (case-insensitive check).
* Check root directory first, then common locations.

### created_at

* Repo created timestamp from GitHub metadata (ISO 8601 string).
* Format: `YYYY-MM-DDTHH:MM:SSZ` (e.g., `2023-01-15T10:30:00Z`).

### last_updated_at

* Prefer `pushed_at` if available; else `updated_at`. Keep ISO 8601 string.
* Format: `YYYY-MM-DDTHH:MM:SSZ`.

### last_updated_by

* Determine from latest commit on `main_branch`:

  * Try commit `author.login` (GitHub user)
  * Fallback to commit `commit.author.name` / email
  * If email is available but no login, format as `name <email>`
* If cannot retrieve: empty string.
* Handle edge case: empty repos with no commits.

### codeowners

* If CODEOWNERS exists (any of these locations):

  * `/CODEOWNERS`, `/.github/CODEOWNERS`, `/docs/CODEOWNERS`
* Output format: `present:false` OR `present:true;owners=@org/team,@user`
* Extract owners by parsing non-comment lines and collecting tokens that start with `@`.
* Handle CODEOWNERS patterns (`*`, `*.py`, `docs/**`) - extract owners from all matching patterns.
* If multiple CODEOWNERS files exist, merge owners from all files.
* Deduplicate and sort alphabetically.
* Handle malformed CODEOWNERS files gracefully (log warning, continue with partial data).

### category

One of: `iac`, `code`, `hybrid`, `pipelines`, `docs`, `misc`, `empty`, `unknown`

Rules (deterministic, evaluated in order):

* `empty`: `total_files == 0` (validate: if total_files == 0, all hit counts must also be 0)
* `docs`: `total_files > 0` AND `doc_hits/total_files > 0.80` AND `iac_hits == 0` AND `code_hits == 0`
* `iac`: `total_files > 0` AND `iac_hits/total_files > 0.60` AND `code_hits/total_files < 0.15`
* `code`: `total_files > 0` AND `code_hits/total_files > 0.60` AND `iac_hits/total_files < 0.10`
* `pipelines`: `total_files > 0` AND `pipeline_hits/total_files > 0.25` AND `iac_hits/total_files < 0.20` AND `code_hits/total_files < 0.20`
* `hybrid`: `iac_hits > 0` AND `code_hits > 0` (and none of the above matched)
* `unknown`: Repo doesn't match any category (safety net)
* else `misc`

**Edge Cases:**

* Handle division by zero explicitly (check `total_files > 0` before division).
* Thresholds are exclusive (e.g., exactly 0.60 doesn't match `> 0.60`).
* If `clone_mode == none`, use heuristics based on repo name and languages only.

### component_type

One of: `micro-service`, `service`, `monolith`, `ui`, `library`, `lambda`, `iac`, `scripts`, `db`, `misc`

Component type represents the architectural/functional role of the repository. This is distinct from `category` (which describes content type) and focuses on what the component does in the system.

**Determination Rules (evaluated in order):**

1. **`iac`**: 
   * `category == iac` OR
   * `iac_hits/total_files > 0.70` OR
   * Repo name contains: `terraform`, `terragrunt`, `pulumi`, `cdk`, `infra`, `infrastructure`, `iac`, `cloudformation`, `helm`, `kustomize`
   * This takes precedence over other types if IaC is the primary purpose.

2. **`lambda`**:
   * Repo name contains: `lambda`, `function`, `serverless`, `faas`
   * OR presence of serverless framework files: `serverless.yml`, `serverless.yaml`, `sam.yaml`, `template.yaml` (AWS SAM), `function.json`
   * OR has `handler.py`, `handler.js`, `index.js` (common Lambda entry points) AND `total_files < 50` (small, focused)
   * OR description contains "lambda", "serverless", "function"

3. **`library`**:
   * `has_package? == true` AND
   * (`total_files < 500` OR `code_hits/total_files > 0.80`) AND
   * Repo name contains: `lib`, `library`, `sdk`, `client`, `package`, `module`, `utils`, `util`
   * OR presence of library-specific files: `setup.py`, `pyproject.toml` (Python libs), `package.json` with `"main"` field (Node libs), `Cargo.toml` (Rust libs)
   * OR description contains "library", "sdk", "client library", "package"

4. **`ui`**:
   * Main language is frontend-focused: `TypeScript`, `JavaScript`, `CSS`, `SCSS`, `HTML`, `Vue`, `React`, `Angular`
   * OR presence of UI framework files: `package.json` with React/Vue/Angular dependencies, `vite.config.*`, `next.config.*`, `nuxt.config.*`, `angular.json`, `webpack.config.*`
   * OR repo name contains: `ui`, `frontend`, `web`, `app`, `client`, `dashboard`, `portal`, `interface`
   * OR directory structure suggests UI: `src/components/`, `src/pages/`, `public/`, `assets/`, `styles/`
   * OR `code_hits/total_files > 0.60` AND main language is `TypeScript`/`JavaScript` AND `iac_hits == 0`

5. **`db`**:
   * Repo name contains: `db`, `database`, `migration`, `schema`, `sql`, `postgres`, `mysql`, `mongo`
   * OR presence of database files: `*.sql`, `migrations/`, `schema/`, `seeds/`, `*.prisma`, `schema.prisma`
   * OR description contains "database", "migration", "schema"

6. **`scripts`**:
   * `total_files < 100` AND
   * (`code_hits/total_files > 0.50` OR main language is `Shell`, `Python`, `Ruby`, `Perl`) AND
   * (`iac_hits == 0` AND `pipeline_hits == 0`) AND
   * (Repo name contains: `script`, `tool`, `util`, `helper`, `bin` OR has `*.sh`, `*.py` files in root)
   * OR `category == misc` AND `total_files < 50` AND main language is scripting language

7. **`micro-service`**:
   * `has_package? == true` AND
   * `total_files >= 50` AND `total_files < 2000` AND
   * (`code_hits/total_files > 0.40` OR main language is application language: `Go`, `Python`, `Java`, `Ruby`, `Node.js`) AND
   * (Presence of service indicators: `Dockerfile`, `docker-compose.yml`, `kubernetes/`, `k8s/`, `deployment.yaml`, `service.yaml`) OR
   * (Repo name contains: `service`, `api`, `microservice`, `ms-`, `svc-`) OR
   * (Description contains "microservice", "micro-service", "service", "API service")

8. **`service`**:
   * `has_package? == true` AND
   * `total_files >= 2000` AND `total_files < 10000` AND
   * `code_hits/total_files > 0.40` AND
   * (Presence of service infrastructure: `Dockerfile`, `kubernetes/`, `deployment.yaml`, `docker-compose.yml`) OR
   * (Repo name contains: `service`, `api`, `backend`, `server`) OR
   * (Description contains "service", "backend service", "API")
   * This is for larger, more complex services that don't fit micro-service size constraints.

9. **`monolith`**:
   * `total_files >= 10000` OR
   * (`code_hits/total_files > 0.50` AND `total_files >= 5000`) AND
   * Multiple main languages detected (>= 3 languages with significant byte counts) OR
   * Repo name contains: `monolith`, `platform`, `app`, `application` (without `micro` prefix) OR
   * (Description contains "monolith", "monolithic", "platform", "full application")
   * This represents large, complex applications with multiple concerns.

10. **`misc`**:
    * Default fallback if none of the above rules match.
    * Used for repos that don't clearly fit into other component types.

**Heuristics Priority:**

* Component type determination should happen **after** category determination.
* If `category == iac`, component_type should typically be `iac` (unless explicitly overridden by stronger signals).
* Repo name and description are strong signals - use case-insensitive matching.
* File structure and presence of framework-specific files are reliable indicators.
* Size thresholds are approximate and can be adjusted based on organizational norms.

**Edge Cases:**

* Empty repos (`total_files == 0`): Set to `misc`.
* If `clone_mode == none`: Use repo name, description, and languages only (less accurate).
* Hybrid repos (both IaC and code): Prefer `iac` if `iac_hits/total_files > 0.50`, otherwise use code-based heuristics.
* Repos with multiple component type indicators: Use the first matching rule in the order above (most specific first).

### risk_signals

Output as a semicolon-separated list of flags (or empty string). Must be **heuristic, non-invasive**. In blobless mode, use file presence/path only. In full mode, allow limited keyword scanning.

**Minimum set of risk flags:**

* `no_readme`: `has_readme? == false`
* `no_codeowners`: CODEOWNERS file not present
* `no_ci`: `pipeline_hits == 0` (no CI/CD detected)
* `no_tags`: No tags found in repository
* `no_releases`: No GitHub releases found (even if tags exist)
* `no_license`: No LICENSE file detected (check common names: `LICENSE`, `LICENSE.txt`, `LICENSE.md`)
* `iac_no_lock`: Terraform repo (has `.tf` files) without `terraform.lock.hcl` OR without backend config pattern
* `secrets_suspected`: Presence of suspicious files: `.env`, `*.pem`, `id_rsa`, `*.p12`, `*.key`, `*.secret`, `credentials`, `secrets.*`, `config/secrets.yml`, `*.p12`, `*.pfx`
* `many_languages`: Number of distinct languages >= 5 (configurable via `--many-languages-threshold`, default 5)
* `large_repo`: `total_files > configurable threshold` (default 5000, via `--large-files-threshold`)
* `stale_repo`: `last_updated_at` older than configurable threshold (default 365 days, via `--stale-days`)
* `hybrid_complexity`: `category == hybrid` AND `total_files > 2000` (configurable via `--hybrid-complexity-threshold`)
* `archived_repo`: Repository is archived (explicit flag)
* `default_branch_not_main`: Default branch is not `main` or `master` (legacy indicator)
* `single_committer`: Only one unique committer detected (bus factor risk, requires commit history analysis - optional, only in full mode)
* `has_dependencies_no_lock`: Has dependency files (`package.json`, `requirements.txt`, etc.) but missing corresponding lock files (`package-lock.json`, `requirements.lock`, etc.)

**Optional (only in full clone mode):**

* `mentions_manual_steps`: README/docs contain keywords: "manual", "clickops", "runbook missing", "todo", "deprecated", "no dr", "break glass", "deprecated", "legacy"
  * Keep scanning bounded (README + docs only), do not scan entire repo.
  * Use case-insensitive matching.
  * Limit to first 10KB of each file to avoid performance issues.

**Risk Signal Formatting:**

* Sort flags alphabetically for consistency.
* Empty string if no flags match.

---

## CLI / Usage Requirements

Implement `repo_inventory.py` with comprehensive argument support:

```bash
python repo_inventory.py --org ORG_NAME --output repo_inventory.csv \
  --clone-mode blobless --clone-root ./repos --include-archived false \
  --concurrency 8 --verbose
```

**Required Arguments:**

* `--org` (required): GitHub organization name

**Optional Arguments:**

* `--output` (default `repo_inventory.csv`): Output CSV file path
* `--clone-mode` (`none|blobless|full`, default `blobless`): Cloning strategy
* `--clone-root` (default `./repos`): Root directory for cloned repositories
* `--include-archived` (default `false`): Include archived repositories
* `--include-forks` (default `false`): Include forked repositories
* `--max-repos` (default unlimited): Maximum number of repos to process
* `--concurrency` (default `8`): Number of concurrent repo processing threads
* `--api-concurrency` (default `10`): Number of concurrent API calls (separate from clone concurrency)
* `--stale-days` (default `365`): Days threshold for stale repo detection
* `--large-files-threshold` (default `5000`): File count threshold for large repo detection
* `--many-languages-threshold` (default `5`): Language count threshold for many_languages flag
* `--hybrid-complexity-threshold` (default `2000`): File count threshold for hybrid_complexity flag
* `--skip-large-repos` (default `100`): Skip repos larger than this size in MB (full clone mode only)
* `--api-timeout` (default `30`): Timeout in seconds for API calls
* `--exclude-prereleases` (default `false`): Exclude pre-releases from latest_tag/latest_release
* `--prefer-ssh` (default `false`): Prefer SSH URLs over HTTPS for clone_url
* `--include-submodules` (default `false`): Include git submodules in file counts
* `--exclude-binary` (default `false`): Exclude binary files from total_files count
* `--cleanup` (default `false`): Remove cloned repositories after scanning
* `--skip-existing` (default `false`): Skip repos that already exist in clone-root (faster re-runs)
* `--resume` (default `false`): Resume from last processed repo (requires `--log-jsonl`)
* `--log-jsonl` (default `None`): Path to JSONL log file for detailed debugging
* `--verbose` / `-v` (default `false`): Enable verbose logging
* `--dry-run` (default `false`): Test API access and validate configuration without cloning
* `--config` (default `None`): Path to configuration file (JSON or YAML) for repeated runs
* `--version` (default `false`): Show version and exit

**Behavior:**

* Always produce CSV even if some repos fail (partial results are acceptable).
* Log per-repo errors to stderr + optional `--log-jsonl` file.
* Idempotent cloning: if repo folder exists and `--skip-existing` is false, attempt `git fetch` + reset shallow if needed.
* Show progress indicator: `Processing repo 15/100 (15%) [ETA: 2m 30s]`
* Print summary statistics at end:
  * Total repos processed
  * Success/failure counts
  * Category breakdown
  * Risk signal frequency
  * Processing time
* Return proper exit codes:
  * `0`: Success (all repos processed)
  * `1`: Partial failure (some repos failed)
  * `2`: Fatal error (cannot proceed)
* Validate prerequisites before starting:
  * Check `gh` is installed (`which gh`)
  * Check `gh` is authenticated (`gh auth status`)
  * Check disk space (warn if < 1GB free)
  * Check write permissions for output directory

**Configuration File Format:**

If `--config` is provided, load settings from JSON/YAML file:

```json
{
  "org": "kompliant",
  "output": "repo_inventory.csv",
  "clone_mode": "blobless",
  "clone_root": "./repos",
  "include_archived": false,
  "concurrency": 8,
  "stale_days": 365,
  "large_files_threshold": 5000
}
```

Command-line arguments override config file values.

---

## Implementation Notes

### Core Implementation

* Use `subprocess.run` to call `gh api` and `git`.
* Use `ThreadPoolExecutor` for per-repo work (API calls + cloning + scanning).
* Separate thread pools for API calls vs git operations (different resource constraints).
* Use `concurrent.futures` for better error handling and progress tracking.

### Error Handling & Resilience

* Implement retry logic with exponential backoff for API calls:
  * Max 3 retries
  * Delays: 1s, 2s, 4s
  * Retry on network errors, 429 (rate limit), 500-599 (server errors)
* Handle partial failures gracefully:
  * Continue processing other repos if one fails
  * Log errors with repo name and error message
  * Include failed repos in CSV with error indicators if possible
* Set timeouts for all operations:
  * API calls: `--api-timeout` seconds (default 30)
  * Git operations: 60 seconds (configurable)
* Validate data before writing:
  * Type checking (integers, booleans, dates)
  * Range validation (non-negative counts)
  * Format validation (ISO 8601 dates)

### Rate Limiting

* `gh` CLI handles authentication and rate limiting, but still throttle:
  * Use `--api-concurrency` to limit concurrent API calls
  * Add small delays between batches if needed
  * Monitor for rate limit headers in responses
* Git operations are less constrained but still limit via `--concurrency`

### CSV Formatting

* RFC4180-compliant CSV:
  * Quote fields containing commas, semicolons, newlines, or quotes
  * Escape quotes by doubling them (`""`)
  * Use UTF-8 encoding
  * Include BOM if `--bom` flag is set (for Excel compatibility)
* Validate CSV before writing:
  * Check all required columns are present
  * Ensure row count matches repo count
  * Verify no encoding issues

### Performance Optimizations

* Cache GitHub API responses during single run (repo metadata doesn't change).
* Stream file listings for large repos (don't load all paths into memory).
* Use `git ls-tree` efficiently (single command, parse output).
* Batch API calls where possible (GitHub supports some batch endpoints).
* Implement incremental updates:
  * `--incremental` mode: only process repos not in existing CSV
  * Compare by repo name + last_updated_at timestamp

### Memory Management

* Avoid reading large files into memory.
* Use generators/iterators for file processing.
* Limit concurrent clones to prevent memory exhaustion.
* Clean up cloned repos promptly if `--cleanup` is set.

### Security Considerations

* Validate GitHub token has necessary scopes (`repo`, `read:org`).
* Sanitize repo names when creating filesystem paths (prevent path traversal).
* Set appropriate file permissions on output files (0600 for sensitive data).
* Never log tokens or credentials.
* Validate all user inputs (org name, paths, thresholds).

### Progress Reporting

* Show real-time progress: `[15/100] Processing: repo-name (15%)`
* Estimate completion time based on average processing speed.
* Log to stderr so stdout can be redirected to file.
* Use structured logging with levels (DEBUG, INFO, WARNING, ERROR).

---

## Testing & Validation Strategy

### Unit Testing

* Test category classification logic with various file distributions.
* Test risk signal detection with sample file patterns.
* Test CSV formatting with edge cases (special characters, newlines, quotes).
* Test CODEOWNERS parsing with various formats.

### Integration Testing

* Test with a small org (`--max-repos 5`) before full run.
* Test with `--dry-run` to validate API access.
* Test with `--clone-mode none` for fast validation.
* Test error handling with invalid org names, network failures.

### Validation

* `--validate` mode: Check CSV format and required columns.
* Verify all repos from org are included (or excluded per flags).
* Check data consistency (e.g., if `total_files == 0`, all hits should be 0).
* Validate date formats are ISO 8601.
* Verify boolean fields are `true`/`false` (not `True`/`False` or `1`/`0`).

---

## Acceptance Criteria

* Produces `repo_inventory.csv` with the exact header and all columns populated as specified.
* Works with private org repos using existing `gh auth login`.
* Default run completes without requiring full clones (blobless mode works).
* Categories match the defined rules (including edge cases).
* Component types are determined accurately based on repo structure, naming patterns, and file signatures.
* Risk signals include at least the minimum flag set.
* Handles errors gracefully (partial results acceptable).
* Shows progress and summary statistics.
* Returns appropriate exit codes.
* Validates prerequisites before starting.
* CSV is RFC4180-compliant and handles special characters correctly.
* Performance: Processes 100 repos in < 10 minutes (blobless mode, 8 concurrency).
* Memory usage: Stays under 500MB for typical runs.

---

## Future Enhancements (Out of Scope)

* Support for multiple GitHub organizations in single run.
* Export to other formats (JSON, Excel, Parquet).
* Integration with GitHub Actions for automated runs.
* Webhook support for incremental updates.
* Risk scoring algorithm (weighted risk signals).
* Comparison mode (compare two inventory runs).
* Visualization dashboard (separate tool).
