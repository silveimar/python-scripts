#!/usr/bin/env python3
"""
Extract code block content from markdown files in prompts-input
and save to prompts-output with cleaned filenames.
"""

import argparse
import os
import re
from pathlib import Path


def extract_code_block(content: str) -> str:
    """
    Extract content from code blocks (```language ... ```).
    Supports any language identifier (jsx, docker, fsharp, etc.) or no identifier.
    
    Args:
        content: The markdown file content
        
    Returns:
        The extracted code block content, or empty string if not found
    """
    # Pattern to match ```[language]...``` code blocks
    # Matches ``` followed by optional language identifier, content, and closing ```
    pattern = r'```[a-zA-Z]*\s*\n(.*?)```'
    match = re.search(pattern, content, re.DOTALL)
    
    if match:
        return match.group(1).strip()
    
    return ""


def add_clarification_instruction(content: str) -> str:
    """
    Add instruction to ask for clarification questions based on 
    the "INFORMATION ABOUT ME" or "ABOUT ME" section.
    
    Args:
        content: The extracted code block content
        
    Returns:
        Content with appended instruction
    """
    # Check if the content has an "INFORMATION ABOUT ME" or "ABOUT ME" section
    if re.search(r'#INFORMATION ABOUT ME:|ABOUT ME:', content, re.IGNORECASE):
        instruction = "\n\n---\n\n**INSTRUCTIONS:** Before proceeding, please ask any clarification questions about the variables in the \"INFORMATION ABOUT ME\" section above to ensure you have all the necessary details to provide an accurate and comprehensive response."
        return content + instruction
    
    return content


def clean_filename(filename: str) -> str:
    """
    Remove alphanumeric suffix from filename.
    
    Example: "Analyze Career Advancement Strategies 2dc860f478858107b569d44c00ca7e38.md"
    becomes: "Analyze Career Advancement Strategies.md"
    
    Args:
        filename: Original filename
        
    Returns:
        Cleaned filename
    """
    # Remove pattern like " 2dc860f478858107b569d44c00ca7e38" before .md
    pattern = r'\s+[a-f0-9]{32}(\.md)$'
    cleaned = re.sub(pattern, r'\1', filename)
    return cleaned


def process_files(input_dir: Path, output_dir: Path):
    """
    Process all markdown files in input directory.
    
    Args:
        input_dir: Directory containing input markdown files
        output_dir: Directory to write processed files
    """
    # Ensure output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Process each markdown file
    processed_count = 0
    skipped_count = 0
    
    for input_file in input_dir.glob('*.md'):
        # Skip .keep files
        if input_file.name == '.keep':
            continue
            
        print(f"Processing: {input_file.name}")
        
        # Read input file
        with open(input_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Extract code block content
        extracted_content = extract_code_block(content)
        
        if not extracted_content:
            print(f"  ⚠️  No code block found in {input_file.name}")
            skipped_count += 1
            continue
        
        # Add clarification instruction
        final_content = add_clarification_instruction(extracted_content)
        
        # Generate output filename
        output_filename = clean_filename(input_file.name)
        output_file = output_dir / output_filename
        
        # Write extracted content to output file
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(final_content)
        
        print(f"  ✓ Created: {output_filename}")
        processed_count += 1
    
    print(f"\n{'='*60}")
    print("Processing complete!")
    print(f"  Processed: {processed_count} files")
    print(f"  Skipped: {skipped_count} files")
    print(f"{'='*60}")


def main():
    """Main entry point."""
    # Set up argument parser
    parser = argparse.ArgumentParser(
        description='Extract code block content from markdown files and save with cleaned filenames.'
    )
    parser.add_argument(
        '-i', '--input',
        type=str,
        help='Input directory containing markdown files (default: ./prompts-input)',
        default=None
    )
    parser.add_argument(
        '-o', '--output',
        type=str,
        help='Output directory for processed files (default: <input-dir>-clean)',
        default=None
    )
    
    args = parser.parse_args()
    
    # Get script directory
    script_dir = Path(__file__).parent
    
    # Define input and output directories
    if args.input:
        input_dir = Path(args.input).resolve()
    else:
        input_dir = script_dir / 'prompts-input'
    
    if args.output:
        output_dir = Path(args.output).resolve()
    else:
        # If output not specified, create it at the same level with -clean suffix
        output_dir = input_dir.parent / f"{input_dir.name}-clean"
    
    # Check if input directory exists
    if not input_dir.exists():
        print(f"Error: Input directory not found: {input_dir}")
        return
    
    print(f"Input directory: {input_dir}")
    print(f"Output directory: {output_dir}")
    print(f"{'='*60}\n")
    
    # Process files
    process_files(input_dir, output_dir)


if __name__ == '__main__':
    main()
