# GitHub Repository Inventory Generator

A local-only Python tool that inventories GitHub repositories and outputs a CSV with detailed metadata, classifications, and risk signals.

## Features

- **Local-only operation**: Runs entirely on your machine using GitHub CLI (`gh`)
- **Comprehensive inventory**: Collects metadata, languages, tags, releases, branches, and more
- **Smart classification**: Automatically categorizes repos and determines component types
- **Risk detection**: Identifies potential issues like missing READMEs, no CI/CD, stale repos, etc.
- **Flexible cloning**: Supports none, blobless, or full clone modes
- **Concurrent processing**: Processes multiple repos in parallel for speed
- **Resilient**: Handles errors gracefully with retry logic and partial results

## Prerequisites

1. **Python 3.7+**
2. **GitHub CLI (`gh`)**: Install from [https://cli.github.com/](https://cli.github.com/)
3. **Git**: Required for cloning repositories
4. **Authentication**: Run `gh auth login` to authenticate with GitHub

## Installation

```bash
# Install Python dependencies
pip install -r requirements.txt

# Make script executable (optional)
chmod +x repo_inventory.py
```

## Usage

### Basic Usage

```bash
python repo_inventory.py --org kompliant
```

This will:
- Fetch all repositories from the `kompliant` organization
- Clone them in blobless mode (fast, no file contents)
- Generate `repo_inventory.csv` with all metadata

### Common Options

```bash
# Specify output file
python repo_inventory.py --org kompliant --output my_inventory.csv

# Include archived repositories
python repo_inventory.py --org kompliant --include-archived

# Process only first 10 repos (for testing)
python repo_inventory.py --org kompliant --max-repos 10

# Filter repos by name patterns
python repo_inventory.py --org kompliant --repo-name-starts-with "terraform-"
python repo_inventory.py --org kompliant --repo-name-contains "api"
python repo_inventory.py --org kompliant --repo-name-pattern "^microservice-.*"

# Use full clone mode (slower but more accurate)
python repo_inventory.py --org kompliant --clone-mode full

# Increase concurrency for faster processing
python repo_inventory.py --org kompliant --concurrency 16

# Adjust rate limiting (if hitting rate limits)
python repo_inventory.py --org kompliant --api-concurrency 5 --rate-limit-delay 0.2

# Enable verbose logging
python repo_inventory.py --org kompliant --verbose

# Dry run (test API access without cloning)
python repo_inventory.py --org kompliant --dry-run
```

### Advanced Usage

```bash
# Use configuration file
python repo_inventory.py --config config.json

# Resume from previous run
python repo_inventory.py --org kompliant --log-jsonl inventory.jsonl --resume

# Clean up cloned repos after scanning
python repo_inventory.py --org kompliant --cleanup

# Skip repos that already exist locally
python repo_inventory.py --org kompliant --skip-existing
```

## Configuration File

Create a JSON or YAML file to store common settings:

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

## Output Format

The tool generates a CSV file with the following columns:

- `repo_name`: Repository name
- `description`: Repository description
- `latest_tag`: Most recent tag (semantic version sorted)
- `has_package?`: Boolean indicating presence of package manifests
- `latest_release`: Latest GitHub release
- `clone_url`: Repository clone URL
- `lanjuages_list`: Semicolon-separated list of languages (e.g., `Ruby:12000;HCL:4000`)
- `main_language`: Primary programming language
- `num_branches`: Number of branches
- `main_branch`: Default branch name
- `category`: Classification (`iac`, `code`, `hybrid`, `pipelines`, `docs`, `misc`, `empty`, `unknown`)
- `component_type`: Component type (`micro-service`, `service`, `monolith`, `ui`, `library`, `lambda`, `iac`, `scripts`, `db`, `misc`)
- `total_files`: Total file count
- `iac_hits`: Count of Infrastructure-as-Code files
- `code_hits`: Count of code files
- `pipeline_hits`: Count of CI/CD pipeline files
- `doc_hits`: Count of documentation files
- `has_readme?`: Boolean indicating README presence
- `created_at`: Repository creation timestamp (ISO 8601)
- `last_updated_at`: Last update timestamp (ISO 8601)
- `last_updated_by`: Author of latest commit
- `codeowners`: CODEOWNERS file status and owners
- `risk_signals`: Semicolon-separated list of risk flags

## Categories

Repositories are classified into categories based on file patterns:

- **`iac`**: Infrastructure-as-Code repositories (>60% IaC files, <15% code)
- **`code`**: Code repositories (>60% code files, <10% IaC)
- **`hybrid`**: Mixed repositories (both IaC and code)
- **`pipelines`**: CI/CD pipeline repositories (>25% pipeline files)
- **`docs`**: Documentation repositories (>80% docs, no code/IaC)
- **`misc`**: Miscellaneous repositories
- **`empty`**: Empty repositories
- **`unknown`**: Unclassified repositories

## Component Types

Component types represent the architectural role:

- **`iac`**: Infrastructure-as-Code repositories
- **`lambda`**: Serverless functions
- **`library`**: Reusable libraries/packages
- **`ui`**: Frontend/user interface applications
- **`db`**: Database/migration repositories
- **`scripts`**: Utility scripts
- **`micro-service`**: Small services (50-2000 files)
- **`service`**: Larger services (2000-10000 files)
- **`monolith`**: Large monolithic applications (10000+ files)
- **`misc`**: Miscellaneous components

## Risk Signals

The tool detects various risk signals:

- `no_readme`: Missing README file
- `no_codeowners`: Missing CODEOWNERS file
- `no_ci`: No CI/CD pipelines detected
- `no_tags`: No git tags found
- `no_releases`: No GitHub releases
- `no_license`: Missing LICENSE file
- `iac_no_lock`: Terraform without lock file
- `secrets_suspected`: Suspicious files that might contain secrets
- `many_languages`: Too many programming languages (configurable threshold)
- `large_repo`: Repository exceeds file count threshold
- `stale_repo`: Repository hasn't been updated recently
- `hybrid_complexity`: Hybrid repo with high complexity
- `archived_repo`: Repository is archived
- `default_branch_not_main`: Using non-standard default branch
- `has_dependencies_no_lock`: Missing lock files for dependencies

## Performance

- **Blobless mode** (default): Processes ~100 repos in <10 minutes
- **Memory usage**: Stays under 500MB for typical runs
- **Concurrency**: Default 8 threads, adjustable via `--concurrency`

## Error Handling

- Partial failures are handled gracefully
- Failed repos are logged but don't stop the entire process
- Exit codes:
  - `0`: Success (all repos processed)
  - `1`: Partial failure (some repos failed)
  - `2`: Fatal error (cannot proceed)

## Troubleshooting

### Authentication Issues

```bash
# Check authentication status
gh auth status

# Re-authenticate if needed
gh auth login
```

### Rate Limiting

The tool includes comprehensive rate limiting protection:

- **Automatic rate limit detection**: Detects 429 errors and waits appropriately
- **Configurable concurrency**: Use `--api-concurrency` to limit concurrent API calls (default: 10)
- **Rate limit delay**: Use `--rate-limit-delay` to add delays between API calls (default: 0.1s)
- **Exponential backoff**: Automatically retries with increasing delays on rate limit errors
- **Semaphore-based throttling**: Limits concurrent API calls to prevent overwhelming the API

If you encounter rate limits:

- Reduce `--api-concurrency` (e.g., `--api-concurrency 5`)
- Increase `--rate-limit-delay` (e.g., `--rate-limit-delay 0.5`)
- Use `--max-repos` to limit scope
- The tool will automatically wait and retry when rate limits are detected

### Disk Space

The tool checks for at least 1GB free space. For large organizations:

- Use `--clone-mode none` for API-only mode
- Use `--cleanup` to remove clones after scanning
- Use `--skip-existing` to reuse existing clones

## Examples

### Quick Test Run

```bash
python repo_inventory.py --org myorg --max-repos 5 --verbose
```

### Filtered Run

```bash
# Process only repos starting with "terraform-"
python repo_inventory.py --org kompliant --repo-name-starts-with "terraform-" --verbose

# Process repos containing "api" in the name
python repo_inventory.py --org kompliant --repo-name-contains "api" --max-repos 20

# Use regex pattern for complex filtering
python repo_inventory.py --org kompliant --repo-name-pattern "^(terraform|pulumi)-.*" --verbose
```

### Full Production Run

```bash
python repo_inventory.py \
  --org kompliant \
  --output inventory.csv \
  --clone-mode blobless \
  --concurrency 16 \
  --log-jsonl inventory.jsonl \
  --verbose
```

### Incremental Update

```bash
# First run
python repo_inventory.py --org kompliant --log-jsonl inventory.jsonl

# Later, resume from where you left off
python repo_inventory.py --org kompliant --log-jsonl inventory.jsonl --resume
```

## License

This tool is provided as-is for local use. Ensure you have proper authorization to access the GitHub organization you're scanning.

## Contributing

When contributing, ensure:

1. All tests pass
2. Code follows PEP 8 style guidelines
3. Error handling is comprehensive
4. Documentation is updated

## Support

For issues or questions:

1. Check the troubleshooting section
2. Review verbose logs (`--verbose`)
3. Check JSONL log file for detailed per-repo information
