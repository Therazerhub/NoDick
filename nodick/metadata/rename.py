"""
Auto-Rename Module for StashDB/FansDB High-Confidence Matches
Handles automatic renaming of video files when match confidence > 90%
"""

import os
import re
import shutil
from typing import Optional, Tuple, Dict, Any
from pathlib import Path

# Default threshold (can be overridden via env)
DEFAULT_RENAME_THRESHOLD = 0.90

# Valid match sources for auto-rename
VALID_SOURCES = {'stashdb', 'fansdb'}

def should_rename(match_score: float, match_source: str, threshold: float = None) -> bool:
    """
    Determine if a file should be renamed based on match score and source.
    
    Args:
        match_score: Match confidence score (0.0 to 1.0)
        match_source: Source of the match ('stashdb', 'fansdb', or 'local')
        threshold: Optional override for the threshold (default: 0.90)
    
    Returns:
        True if score > threshold and source is StashDB or FansDB
    """
    if threshold is None:
        threshold = float(os.getenv('AUTO_RENAME_THRESHOLD', DEFAULT_RENAME_THRESHOLD))
    
    # Must be from a valid database source
    if match_source.lower() not in VALID_SOURCES:
        return False
    
    # Must exceed threshold
    return match_score >= threshold


def sanitize_filename(text: str) -> str:
    """
    Sanitize text for use in filenames.
    Removes or replaces characters that are invalid in filenames.
    
    Args:
        text: Text to sanitize
    
    Returns:
        Sanitized text safe for filenames
    """
    if not text:
        return "Unknown"
    
    # Replace common invalid characters
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        text = text.replace(char, '')
    
    # Replace control characters
    text = ''.join(char for char in text if ord(char) >= 32)
    
    # Strip leading/trailing whitespace and dots
    text = text.strip('. ')
    
    # Replace multiple spaces with single space
    text = re.sub(r'\s+', ' ', text)
    
    # Limit length to avoid path too long errors
    if len(text) > 100:
        text = text[:97] + "..."
    
    return text if text else "Unknown"


def get_female_performer_names(performers: list, max_performers: int = 2) -> str:
    """
    Extract female performer names from scene data.
    
    Args:
        performers: List of performer objects from scene data
        max_performers: Maximum number of performers to include
    
    Returns:
        Formatted string of performer names
    """
    if not performers:
        return "Unknown"
    
    female_names = []
    
    for p in performers:
        if not isinstance(p, dict):
            continue
        
        performer = p.get('performer', {})
        if not isinstance(performer, dict):
            continue
        
        name = performer.get('name', '')
        gender = performer.get('gender', '')
        
        # Only include if female (FEMALE or empty/unknown)
        if name and (not gender or gender.upper() in ['FEMALE', 'F', '']):
            female_names.append(name)
            if len(female_names) >= max_performers:
                break
    
    if len(female_names) == 0:
        # Fall back to any performer if no females found
        for p in performers:
            if isinstance(p, dict):
                performer = p.get('performer', {})
                if isinstance(performer, dict):
                    name = performer.get('name', '')
                    if name:
                        female_names.append(name)
                        if len(female_names) >= max_performers:
                            break
    
    if len(female_names) == 1:
        return female_names[0]
    elif len(female_names) == 2:
        return f"{female_names[0]} & {female_names[1]}"
    elif len(female_names) > 2:
        return f"{female_names[0]} & {len(female_names) - 1} others"
    
    return "Unknown"


def generate_new_filename(scene_data: Dict[str, Any], original_filename: str) -> str:
    """
    Generate a new filename based on scene data.
    Format: "Studio - Performer - Title [StashID].ext"
    
    Args:
        scene_data: Scene data from StashDB/FansDB
        original_filename: Original filename (used for extension)
    
    Returns:
        New filename with proper formatting
    """
    # Extract original extension
    original_ext = Path(original_filename).suffix.lower()
    if not original_ext:
        original_ext = '.mp4'  # Default extension
    
    # Get studio name
    studio_obj = scene_data.get('studio', {})
    studio_name = ''
    if isinstance(studio_obj, dict):
        studio_name = studio_obj.get('name', '')
    studio_name = sanitize_filename(studio_name)
    
    # Get performer names
    performers = scene_data.get('performers', [])
    performer_str = get_female_performer_names(performers)
    performer_str = sanitize_filename(performer_str)
    
    # Get title
    title = scene_data.get('title', '')
    title = sanitize_filename(title)
    
    # Get StashID
    stash_id = scene_data.get('id', '')
    
    # Build new filename
    parts = []
    if studio_name and studio_name != "Unknown":
        parts.append(studio_name)
    if performer_str and performer_str != "Unknown":
        parts.append(performer_str)
    if title and title != "Unknown":
        parts.append(title)
    
    # If we don't have enough info, use original name minus extension
    if len(parts) < 2:
        base_name = Path(original_filename).stem
        parts = [sanitize_filename(base_name)]
    
    # Join parts with " - " separator
    new_name = ' - '.join(parts)
    
    # Add StashID in brackets if available
    if stash_id:
        new_name = f"{new_name} [{stash_id}]"
    
    # Add extension
    new_filename = f"{new_name}{original_ext}"
    
    return new_filename


def get_unique_filename(directory: str, filename: str) -> str:
    """
    Generate a unique filename by adding a number suffix if file exists.
    
    Args:
        directory: Target directory
        filename: Desired filename
    
    Returns:
        Unique filename (may have number suffix)
    """
    base_path = Path(directory) / filename
    
    if not base_path.exists():
        return filename
    
    # Split filename into name and extension
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    
    # Find next available number
    counter = 1
    while True:
        new_filename = f"{stem} ({counter}){suffix}"
        new_path = Path(directory) / new_filename
        
        if not new_path.exists():
            return new_filename
        
        counter += 1
        
        # Safety limit
        if counter > 999:
            # Add timestamp to ensure uniqueness
            import time
            new_filename = f"{stem}_{int(time.time())}{suffix}"
            return new_filename


def rename_file(old_path: str, new_filename: str) -> Tuple[bool, str]:
    """
    Safely rename a file, handling collisions.
    
    Args:
        old_path: Full path to the original file
        new_filename: New filename (not full path)
    
    Returns:
        Tuple of (success: bool, result: str)
        - On success: (True, new_full_path)
        - On failure: (False, error_message)
    """
    try:
        old_path = Path(old_path)
        
        if not old_path.exists():
            return False, f"File not found: {old_path}"
        
        directory = old_path.parent
        
        # Get unique filename if collision exists
        unique_filename = get_unique_filename(str(directory), new_filename)
        new_path = directory / unique_filename
        
        # Perform rename
        shutil.move(str(old_path), str(new_path))
        
        return True, str(new_path)
    
    except PermissionError:
        return False, f"Permission denied: unable to rename {old_path}"
    except OSError as e:
        return False, f"OS error renaming file: {e}"
    except Exception as e:
        return False, f"Unexpected error renaming file: {e}"


def move_to_organized_folder(filename: str, studio_name: str, base_folder: str = None) -> Tuple[bool, str]:
    """
    Move a file to an organized folder structure by studio.
    
    Args:
        filename: Current full path to the file
        studio_name: Name of the studio (for folder naming)
        base_folder: Base folder for organization (default: same as file location)
    
    Returns:
        Tuple of (success: bool, result: str)
        - On success: (True, new_full_path)
        - On failure: (False, error_message)
    """
    try:
        file_path = Path(filename)
        
        if not file_path.exists():
            return False, f"File not found: {filename}"
        
        # Determine base folder
        if base_folder is None:
            base_folder = file_path.parent
        else:
            base_folder = Path(base_folder)
            # Create base folder if it doesn't exist
            base_folder.mkdir(parents=True, exist_ok=True)
        
        # Sanitize studio name for folder
        studio_folder = sanitize_filename(studio_name) if studio_name else "Unknown"
        
        # Create studio folder
        studio_path = base_folder / studio_folder
        studio_path.mkdir(parents=True, exist_ok=True)
        
        # Get unique filename in destination
        unique_filename = get_unique_filename(str(studio_path), file_path.name)
        new_path = studio_path / unique_filename
        
        # Move file
        shutil.move(str(file_path), str(new_path))
        
        return True, str(new_path)
    
    except PermissionError:
        return False, f"Permission denied: unable to move {filename}"
    except OSError as e:
        return False, f"OS error moving file: {e}"
    except Exception as e:
        return False, f"Unexpected error moving file: {e}"


def get_rename_suggestion(scene_data: Dict[str, Any], original_filename: str) -> Dict[str, str]:
    """
    Get a rename suggestion with before/after filenames.
    
    Args:
        scene_data: Scene data from StashDB/FansDB
        original_filename: Original filename
    
    Returns:
        Dictionary with rename details:
        {
            'original': original filename,
            'suggested': suggested new filename,
            'studio': studio name,
            'performer': performer name,
            'title': scene title,
            'stash_id': stash ID
        }
    """
    # Get studio name
    studio_obj = scene_data.get('studio', {})
    studio_name = ''
    if isinstance(studio_obj, dict):
        studio_name = studio_obj.get('name', '')
    
    # Get performer names
    performers = scene_data.get('performers', [])
    performer_str = get_female_performer_names(performers)
    
    # Get title
    title = scene_data.get('title', '')
    
    # Get StashID
    stash_id = scene_data.get('id', '')
    
    # Generate new filename
    new_filename = generate_new_filename(scene_data, original_filename)
    
    return {
        'original': original_filename,
        'suggested': new_filename,
        'studio': studio_name,
        'performer': performer_str,
        'title': title,
        'stash_id': stash_id
    }


def is_auto_rename_enabled() -> bool:
    """Check if auto-rename is enabled in environment."""
    return os.getenv('AUTO_RENAME_ENABLED', 'false').lower() == 'true'


def is_organize_by_studio_enabled() -> bool:
    """Check if organize by studio is enabled in environment."""
    return os.getenv('ORGANIZE_BY_STUDIO', 'false').lower() == 'true'


def get_organized_folder_path() -> Optional[str]:
    """Get the base path for organized folders from environment."""
    path = os.getenv('ORGANIZED_FOLDER_PATH', '')
    return path if path else None


# Test function
if __name__ == '__main__':
    # Test cases
    print("=" * 70)
    print("AUTO-RENAME MODULE TEST")
    print("=" * 70)
    
    # Test should_rename
    print("\n1. Testing should_rename():")
    print(f"   Score 0.95, stashdb: {should_rename(0.95, 'stashdb')}")  # True
    print(f"   Score 0.85, stashdb: {should_rename(0.85, 'stashdb')}")  # False
    print(f"   Score 0.95, local: {should_rename(0.95, 'local')}")      # False
    print(f"   Score 0.95, fansdb: {should_rename(0.95, 'fansdb')}")    # True
    
    # Test sanitize_filename
    print("\n2. Testing sanitize_filename():")
    print(f"   'Brazzers: Test <Video>': '{sanitize_filename('Brazzers: Test <Video>')}'")
    print(f"   'J Mac / Riley Reid': '{sanitize_filename('J Mac / Riley Reid')}'")
    print(f"   'Title with   spaces': '{sanitize_filename('Title with   spaces')}'")
    
    # Test generate_new_filename
    print("\n3. Testing generate_new_filename():")
    test_scene = {
        'id': 'abc-123-def',
        'title': 'Big Tits at Work',
        'studio': {'name': 'Brazzers'},
        'performers': [
            {'performer': {'name': 'J Mac', 'gender': 'MALE'}},
            {'performer': {'name': 'Riley Reid', 'gender': 'FEMALE'}}
        ]
    }
    original = "brazzers_bigtits_jmac_riley.mp4"
    new_name = generate_new_filename(test_scene, original)
    print(f"   Original: {original}")
    print(f"   New:      {new_name}")
    
    # Test with multiple performers
    test_scene2 = {
        'id': 'xyz-789',
        'title': 'Threesome Fun',
        'studio': {'name': 'Naughty America'},
        'performers': [
            {'performer': {'name': 'Ariana Marie', 'gender': 'FEMALE'}},
            {'performer': {'name': 'Gabbie Carter', 'gender': 'FEMALE'}},
            {'performer': {'name': 'John Doe', 'gender': 'MALE'}}
        ]
    }
    original2 = "naughty_threesome.mkv"
    new_name2 = generate_new_filename(test_scene2, original2)
    print(f"\n   Original: {original2}")
    print(f"   New:      {new_name2}")
    
    # Test get_rename_suggestion
    print("\n4. Testing get_rename_suggestion():")
    suggestion = get_rename_suggestion(test_scene, original)
    print(f"   Original:  {suggestion['original']}")
    print(f"   Suggested: {suggestion['suggested']}")
    print(f"   Studio:    {suggestion['studio']}")
    print(f"   Performer: {suggestion['performer']}")
    print(f"   Title:     {suggestion['title']}")
    print(f"   StashID:   {suggestion['stash_id']}")
    
    print("\n" + "=" * 70)
    print("TEST COMPLETE")
    print("=" * 70)
