# Confluence Space to Markdown Exporter

A Python script that exports an entire Confluence space to well-structured Markdown files, preserving hierarchy, attachments, internal links, and metadata.

## Features

- **Full Space Export**: Exports all pages from a specified Confluence space
- **Hierarchy Preservation**: Maintains page hierarchy using folder structure based on parent-child relationships
- **Attachment Handling**: Downloads and properly links all page attachments (images, documents, etc.)
- **Link Conversion**: Converts Confluence internal links to relative Markdown links
- **Image Support**: Handles both attached images and external images
- **Metadata Preservation**: Adds YAML front matter with page title, ID, labels, and version
- **Macro Cleanup**: Removes or converts Confluence-specific macros
- **HTML to Markdown**: Uses Pandoc (system or pypandoc) for high-quality conversion
- **Rate Limiting**: Includes sleep delays to avoid API throttling

## Requirements

### System Requirements
- Python 3.6+
- **Pandoc** (recommended): Install via `brew install pandoc` (macOS) or from [pandoc.org](https://pandoc.org/installing.html)

### Python Dependencies
```bash
pip install -r requirements.txt
```

Required packages:
- `requests` - HTTP requests to Confluence API
- `beautifulsoup4` - HTML parsing and manipulation
- `PyYAML` - YAML front matter generation
- `pypandoc` (optional) - Python wrapper for Pandoc

## Configuration

### Authentication
You need a Confluence API token to use this script:

1. Go to [Atlassian Account Settings](https://id.atlassian.com/manage-profile/security/api-tokens)
2. Create an API token
3. Set environment variables:
   ```bash
   export CONFLUENCE_EMAIL="your-email@example.com"
   export CONFLUENCE_API_TOKEN="your-api-token"
   ```

Alternatively, pass credentials via command-line arguments.

## Usage

### Basic Usage
```bash
python confluence-to-md.py --space SPACEKEY
```

### Full Options
```bash
python confluence-to-md.py \
  --url "https://your-domain.atlassian.net/wiki" \
  --space SPACEKEY \
  --email "your-email@example.com" \
  --token "your-api-token" \
  --output "output_directory" \
  --pypandoc
```

### Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `--url` | Confluence base URL | `https://legitscript.atlassian.net/wiki` |
| `--space` | **Required** - Space key to export | - |
| `--email` | Authentication email | `$CONFLUENCE_EMAIL` |
| `--token` | API token | `$CONFLUENCE_API_TOKEN` |
| `--output` | Output directory | `confluence_export` |
| `--pypandoc` | Use pypandoc library instead of system pandoc | `False` |

## Output Structure

The script creates a folder structure that mirrors your Confluence space hierarchy:

```
output_directory/
├── Parent Page.md
├── Parent Page/
│   ├── Child Page 1.md
│   ├── Child Page 1/
│   │   ├── _attachments/
│   │   │   └── 123456/
│   │   │       ├── image.png
│   │   │       └── document.pdf
│   │   └── Grandchild Page.md
│   └── Child Page 2.md
└── Another Root Page.md
```

### File Format

Each Markdown file includes YAML front matter:

```markdown
---
title: Page Title
id: '123456'
labels:
  - label1
  - label2
version: 5
---

# Page Content

Content with properly converted links and images...
```

## How It Works

1. **Fetch Pages**: Retrieves all pages in the space via REST API
2. **Build Hierarchy Map**: Creates a mapping of page IDs to file paths based on ancestors
3. **Process Each Page**:
   - Fetches full page content and metadata
   - Downloads all attachments to `_attachments/{page-id}/` folder
   - Rewrites HTML content:
     - Converts Confluence image macros to standard `<img>` tags
     - Converts internal page links to relative paths
     - Updates attachment references to local paths
     - Removes or converts Confluence macros
   - Converts HTML to Markdown using Pandoc
   - Adds YAML front matter
4. **Output**: Saves Markdown files in hierarchical folder structure

## Troubleshooting

### Pandoc Not Found
If you see warnings about Pandoc, install it:
```bash
# macOS
brew install pandoc

# Ubuntu/Debian
sudo apt-get install pandoc

# Windows
choco install pandoc
```

Or use the `--pypandoc` flag to use the Python library instead.

### Authentication Failed
- Verify your API token is valid
- Ensure email matches your Atlassian account
- Check that the base URL is correct for your instance

### Rate Limiting
If you encounter rate limiting errors, the script includes built-in delays (`sleep_between_requests = 0.1`). You can adjust this in the code if needed.

### Missing Attachments
- Ensure you have read permissions for all attachments
- Check that attachment paths don't contain invalid characters
- Large attachments may fail to download - check logs for errors

## Limitations

- **Confluence Server vs Cloud**: Designed for Confluence Cloud; may need adjustments for Server
- **Complex Macros**: Some advanced Confluence macros may not convert perfectly
- **Filename Conflicts**: Pages with identical names are distinguished by appending page ID
- **Binary Content**: Non-text attachments are downloaded but not converted

## Security

- API tokens are sensitive - never commit them to version control
- Use environment variables or secure secret management
- The script includes basic sanitization to prevent path traversal attacks

## Contributing

Contributions welcome! Consider adding:
- Support for Confluence Server
- Better macro conversion
- Incremental updates (only export changed pages)
- Progress bars for large exports

## License

This script is provided as-is for internal use.
