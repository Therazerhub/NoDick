"""
StashDB/FansDB Integration Module
Handles caption formatting with metadata lookup - ENHANCED with rapidfuzz
"""

import os
import re
import requests
from typing import Optional, Dict, List, Tuple
# Import auto-rename utilities
from nodick.metadata.rename import (
    should_rename,
    generate_new_filename,
    get_rename_suggestion,
    is_auto_rename_enabled
)

# Import new matching utilities
from nodick.metadata.matching import (
    calculate_similarity,
    combined_similarity,
    enhanced_performer_match,
    calculate_match_confidence,
    ngram_similarity,
    normalize_single_char_name,
    extract_performer_candidates,
    build_enhanced_queries,
    handle_concatenated_title,
    RAPIDFUZZ_AVAILABLE,
    JELLYFISH_AVAILABLE
)

# Import local performer database for instant lookups
from nodick.metadata.performer_db import PerformerDB

from nodick.config import settings

# Similarity threshold (0.0 to 1.0) - can be changed dynamically
MATCH_THRESHOLD = 0.0  # 0 = no filtering, show all StashDB results

# Debug mode - logs which algorithms contributed to matches
DEBUG_MATCHING = settings.debug_matching

def set_match_threshold(value: float):
    """Set the match threshold dynamically"""
    global MATCH_THRESHOLD
    MATCH_THRESHOLD = max(0.0, min(1.0, value))
    return MATCH_THRESHOLD

def get_match_threshold() -> float:
    """Get current match threshold"""
    return MATCH_THRESHOLD


def set_debug_mode(enabled: bool):
    """Enable/disable debug logging for matching"""
    global DEBUG_MATCHING
    DEBUG_MATCHING = enabled
    return DEBUG_MATCHING


# ============================================================
# LOCAL PERFORMER DATABASE INTEGRATION
# ============================================================

# Module-level PerformerDB instance (lazy loaded)
_performer_db = None

def get_performer_db() -> Optional[PerformerDB]:
    """
    Get or create the local performer database instance.
    Returns None if database is not initialized.
    """
    global _performer_db
    
    if _performer_db is None:
        try:
            db = PerformerDB()
            # Check if database is initialized by getting stats
            stats = db.get_stats()
            if stats.get('total_performers', 0) > 0:
                _performer_db = db
            else:
                print("⚠️  Local performer database is empty. Run: python update_performer_db.py")
                db._close()
                return None
        except Exception as e:
            print(f"⚠️  Local performer database error: {e}")
            return None
    
    return _performer_db


def search_performer_local(name: str, limit: int = 5, min_score: float = 0.5) -> List[Tuple[str, str, float]]:
    """
    Search for performers in the local database.
    
    Args:
        name: Performer name to search for
        limit: Maximum number of results
        min_score: Minimum similarity score (0.0 to 1.0)
        
    Returns:
        List of tuples (stashdb_id, name, score)
    """
    db = get_performer_db()
    if db is None:
        return []
    
    results = db.search_performer(name, limit=limit, min_score=min_score)
    return [(p.stashdb_id, p.name, score) for p, score in results]


def get_performer_by_stashdb_id(stashdb_id: str) -> Optional[Dict]:
    """
    Get performer details by StashDB ID from local database.
    
    Args:
        stashdb_id: The StashDB performer ID
        
    Returns:
        Performer data dict or None if not found
    """
    db = get_performer_db()
    if db is None:
        return None
    
    performer = db.get_performer_by_id(stashdb_id)
    if performer:
        return {
            'id': performer.stashdb_id,
            'name': performer.name,
            'gender': performer.gender,
            'aliases': performer.aliases,
            'images': performer.images
        }
    return None


def local_performer_search_available() -> bool:
    """Check if local performer database is available and populated"""
    db = get_performer_db()
    return db is not None


# Legacy function for backward compatibility - now uses rapidfuzz
def calculate_similarity_legacy(str1: str, str2: str) -> float:
    """Calculate similarity ratio between two strings (0.0 to 1.0) - LEGACY"""
    return calculate_similarity(str1, str2)


# Both APIs
STASHDB_API_KEY = settings.stashdb_api_key
STASHDB_GRAPHQL_URL = settings.stashdb_graphql_url

FANSDB_API_KEY = settings.fansdb_api_key
FANSDB_GRAPHQL_URL = settings.fansdb_graphql_url

# GraphQL query for FansDB
FANSDB_SEARCH_QUERY = """
query SearchScenes($input: SceneQueryInput!) {
  queryScenes(input: $input) {
    scenes {
      id
      title
      performers {
        performer {
          name
          gender
        }
      }
      studio {
        name
      }
      tags {
        name
      }
    }
  }
}
"""

# GraphQL query for StashDB
STASHDB_SEARCH_QUERY = """
query SearchScenes($term: String!, $limit: Int = 5) {
  searchScene(term: $term, limit: $limit) {
    id
    title
    performers {
      performer {
        name
        gender
      }
    }
    studio {
      name
    }
    tags {
      name
    }
  }
}
"""

# ============================================================
# COMPREHENSIVE STUDIO DETECTION
# ============================================================

STUDIO_PATTERNS = {
    'Brazzers': {
        'domains': ['brazzers', 'brazzersexxtra', 'bex'],
        'aliases': ['brazzer', 'bzz'],
    },
    'Naughty America': {
        'domains': ['naughtyamerica', 'naughtyamericavr'],
        'aliases': ['naughty', 'mywife', 'mywifeshotfriend', 'mynaughtymassage', 
                   'myfirstsexteacher', 'naughtyoffice', 'mydadshotgirlfriend',
                   'myfriendshotmom', 'askyourmother', 'tigermoms'],
    },
    'Blacked': {
        'domains': ['blacked'],
        'aliases': ['blkd'],
    },
    'Blacked Raw': {
        'domains': ['blackedraw'],
        'aliases': ['blkdraw'],
    },
    'Tushy': {
        'domains': ['tushy'],
        'aliases': [],
    },
    'Tushy Raw': {
        'domains': ['tushyraw'],
        'aliases': ['tushyraw'],
    },
    'Vixen': {
        'domains': ['vixen'],
        'aliases': ['vxn'],
    },
    'Deeper': {
        'domains': ['deeper'],
        'aliases': [],
    },
    'Slayed': {
        'domains': ['slayed'],
        'aliases': [],
    },
    'Bang Bros': {
        'domains': ['bangbros', 'bangbus', 'bangpov'],
        'aliases': ['bros', 'bangbus', 'bangpov'],
    },
    'Reality Kings': {
        'domains': ['realitykings', 'rk', 'rkprime'],
        'aliases': ['rk', 'rkprime', 'realityking'],
    },
    'Mofos': {
        'domains': ['mofos'],
        'aliases': [],
    },
    'Digital Playground': {
        'domains': ['digitalplayground'],
        'aliases': ['dp'],
    },
    'Evil Angel': {
        'domains': ['evilangel'],
        'aliases': ['ea'],
    },
    'Jules Jordan': {
        'domains': ['julesjordan'],
        'aliases': ['jj'],
    },
    'Team Skeet': {
        'domains': ['teamskeet'],
        'aliases': ['ts'],
    },
    'Fake Hub': {
        'domains': ['fakehub', 'faketaxi', 'fakeagent'],
        'aliases': ['fake', 'faketaxi', 'fakeagent'],
    },
    'Sweet Sinner': {
        'domains': ['sweetsinner'],
        'aliases': [],
    },
    'Milfed': {
        'domains': ['milfed'],
        'aliases': [],
    },
    'Penthouse': {
        'domains': ['penthouse'],
        'aliases': ['pth'],
    },
    '21Sextury': {
        'domains': ['21sextury', '21sext'],
        'aliases': ['21sext'],
    },
}

def detect_studio(filename: str) -> tuple:
    """
    Detect studio from filename using patterns and aliases.
    Returns (studio_name, cleaned_filename) or (None, filename)
    """
    # Clean filename for matching
    clean_name = filename.lower()
    clean_name = re.sub(r'\.(mp4|mkv|avi|mov|wmv|flv|webm|m4v|mpeg|mpg)$', '', clean_name)
    
    # Check each studio
    for studio_name, patterns in STUDIO_PATTERNS.items():
        # Check domains
        for domain in patterns['domains']:
            # Match at start or after separator
            pattern = rf'(?:^|[\s\-_\.]){re.escape(domain)}(?:[\s\-_\.]|$)'
            if re.search(pattern, clean_name, re.IGNORECASE):
                # Remove studio from filename
                cleaned = re.sub(pattern, ' ', clean_name, flags=re.IGNORECASE)
                cleaned = re.sub(r'\s+', ' ', cleaned).strip()
                return studio_name, cleaned
        
        # Check aliases
        for alias in patterns['aliases']:
            pattern = rf'(?:^|[\s\-_\.]){re.escape(alias)}(?:[\s\-_\.]|$)'
            if re.search(pattern, clean_name, re.IGNORECASE):
                cleaned = re.sub(pattern, ' ', clean_name, flags=re.IGNORECASE)
                cleaned = re.sub(r'\s+', ' ', cleaned).strip()
                return studio_name, cleaned
    
    # Check for generic patterns like "Studio - Performer - Title"
    dash_pattern = r'^([a-z\s]+)\s*-\s*(.+)$'
    match = re.match(dash_pattern, clean_name, re.IGNORECASE)
    if match:
        potential_studio = match.group(1).strip()
        rest = match.group(2).strip()
        # Check if first part is a known studio
        for studio_name in STUDIO_PATTERNS.keys():
            if studio_name.lower() in potential_studio or potential_studio in studio_name.lower():
                return studio_name, rest
    
    return None, filename


def query_api(url: str, api_key: str, query: str, variables: dict = None) -> Optional[dict]:
    """Make a GraphQL query to an API"""
    if not api_key:
        return None
    
    headers = {
        'Content-Type': 'application/json',
        'ApiKey': api_key  # StashDB/FansDB use ApiKey header, not Bearer
    }
    
    payload = {'query': query, 'variables': variables or {}}
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if 'errors' in data:
            print(f"GraphQL errors: {data['errors']}")
            return None
            
        return data
    except Exception as e:
        print(f"API error: {e}")
        return None


def search_fansdb(title: str, performer: str = None) -> Optional[dict]:
    """Search FansDB for a scene"""
    if not FANSDB_API_KEY:
        return None
    
    # Clean search text - remove dots, special chars
    search_text = f"{performer} {title}" if performer else title
    search_text = re.sub(r'[^\w\s]', ' ', search_text)  # Remove special chars
    search_text = re.sub(r'\s+', ' ', search_text).strip()  # Normalize spaces
    
    result = query_api(FANSDB_GRAPHQL_URL, FANSDB_API_KEY, FANSDB_SEARCH_QUERY, 
                      {'input': {'text': search_text, 'per_page': 5}})
    
    if result and 'data' in result:
        scenes = result['data'].get('queryScenes', {}).get('scenes', [])
        if scenes:
            return scenes[0]
    return None


def search_stashdb(title: str, performer: str = None) -> Optional[dict]:
    """Search StashDB for a scene"""
    if not STASHDB_API_KEY:
        return None
    
    # Clean search term - remove dots, special chars
    search_term = f"{performer} {title}" if performer else title
    search_term = re.sub(r'[^\w\s]', ' ', search_term)  # Remove special chars
    search_term = re.sub(r'\s+', ' ', search_term).strip()  # Normalize spaces
    
    result = query_api(STASHDB_GRAPHQL_URL, STASHDB_API_KEY, STASHDB_SEARCH_QUERY, 
                      {'term': search_term, 'limit': 5})
    
    if result and 'data' in result:
        scenes = result['data'].get('searchScene', [])
        if scenes:
            return scenes[0]
    return None


def extract_domains(filename: str) -> list:
    """Extract all domain names from filename, return list of (domain, clean_name) tuples"""
    # Only strip real file extensions (not .com, .net, etc which are part of the name)
    # Match known video extensions at the end
    name = re.sub(r'\.(mp4|mkv|avi|mov|wmv|flv|webm|m4v|mpeg|mpg)$', '', filename, flags=re.IGNORECASE)
    
    # Find all domain patterns: word.com, word.net, etc.
    domain_pattern = r'([a-zA-Z0-9]+)\.(com|net|org|cc|co\.\w+|tv|xxx)'
    matches = re.findall(domain_pattern, name, flags=re.IGNORECASE)
    
    domains = []
    for match in matches:
        domain_name = match[0].lower()
        domains.append(domain_name)
    
    # Return list of (domain, None) - we'll clean per-domain later
    return [(d, None) for d in domains]


def parse_filename_with_domain(filename: str, domain: str = None) -> tuple:
    """Parse filename to extract performer and title, keeping only the specified domain
    NEW: Also detects and returns studio name, handles concatenated words and single-char names
    """
    # Only strip real video extensions
    name = re.sub(r'\.(mp4|mkv|avi|mov|wmv|flv|webm|m4v|mpeg|mpg)$', '', filename, flags=re.IGNORECASE)
    
    # Detect studio FIRST (before cleaning domains)
    detected_studio, name = detect_studio(name)
    
    # Extract ALL domains first
    all_domains = extract_domains(filename)
    all_domain_names = [d[0] for d in all_domains]
    
    # Remove ALL domains except the one we're keeping
    for d in all_domain_names:
        if d != domain:  # Remove other domains
            name = re.sub(rf'\b{re.escape(d)}\.(com|net|org|cc|co\.\w+|tv|xxx)\b', '', name, flags=re.IGNORECASE)
    
    # Also remove the domain we're keeping (but keep its name as studio reference)
    if domain:
        name = re.sub(rf'\b{re.escape(domain)}\.(com|net|org|cc|co\.\w+|tv|xxx)\b', '', name, flags=re.IGNORECASE)
    else:
        # Remove ALL domains if no specific domain specified
        name = re.sub(r'\b\w+\.(com|net|org|cc|co\.\w+|tv|xxx)\b', '', name, flags=re.IGNORECASE)
    
    # Replace remaining dots and underscores with spaces
    name = name.replace('.', ' ').replace('_', ' ')
    
    # Remove date patterns  
    name = re.sub(r'\b(19|20)?\d{2}\s+\d{1,2}\s+\d{1,2}\b', '', name)
    name = re.sub(r'\b\d{1,2}\s+\d{1,2}\s+(19|20)?\d{2}\b', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    
    # Handle single-letter first names (J Mac -> JMac normalization)
    name = normalize_single_char_name(name)
    
    # Handle common patterns
    # Pattern 1: "Studio - Performer - Title" (with dashes)
    if ' - ' in name:
        parts = name.split(' - ')
        if len(parts) >= 3:
            performer = parts[1].strip()
            title = ' - '.join(parts[2:]).strip()
            # Handle concatenated words in title
            title = handle_concatenated_title(title)
            return performer, title, detected_studio
        elif len(parts) == 2:
            return parts[0].strip(), parts[1].strip(), detected_studio
    
    # Pattern 2: Detect studio at start (already handled by detect_studio, but check for more)
    words = name.split()
    if len(words) >= 3:
        first_word = words[0].lower()
        # Common studios (backup check)
        studios = ['brazzers', 'brazzersexxtra', 'vixen', 'tushy', 'blacked', 'blackedraw',
                   'sweetsinner', 'naughtyamerica', 'naughty', 'bangbros', 'bang', 
                   'realitykings', 'rk', 'mofos', 'digitalplayground', 'dp', 'evilangel',
                   'julesjordan', 'teamskeet', 'fakehub', 'deeper', 'tushyraw',
                   'slayed', 'milf', 'mom', 'sis', 'step', 'mydadshotgirlfriend',
                   'naughtyoffice', 'myfirstsexteacher', 'myfriendshotmom',
                   'mywife', 'mynaughty', 'mydirty', 'askyourmother', 'tigermoms']
        
        if first_word in studios:
            remaining = ' '.join(words[1:]).strip()
            remaining_words = remaining.split()
            
            if len(remaining_words) >= 2:
                performer = ' '.join(remaining_words[:2])
                title = ' '.join(remaining_words[2:]) if len(remaining_words) > 2 else "Scene"
                title = handle_concatenated_title(title)
                return performer, title, detected_studio
            elif len(remaining_words) == 1:
                return remaining_words[0], "Scene", detected_studio
    
    # Pattern 3: Simple "Performer Title"
    words = name.split()
    if len(words) >= 3:
        performer = ' '.join(words[:2])
        title = ' '.join(words[2:])
        title = handle_concatenated_title(title)
        return performer, title, detected_studio
    elif len(words) >= 2:
        return words[0], ' '.join(words[1:]), detected_studio
    
    return None, name, detected_studio


def parse_filename(filename: str) -> tuple:
    """Parse filename to extract performer, title, and studio"""
    return parse_filename_with_domain(filename, domain=None)


# Legacy functions for backward compatibility
def get_path_words(filename: str) -> list:
    """Stash-style: Extract searchable words from filename (first 2 chars technique)"""
    return get_path_words(filename)


def build_smart_queries(filename: str) -> list:
    """Build multiple search queries for better matching"""
    # Parse filename to get performer and title
    performer, title, _ = parse_filename_with_domain(filename, None)
    return build_enhanced_queries(filename, performer=performer, title=title)


# Track API usage
api_call_count = 0
MAX_API_CALLS = 20  # Safety limit


def check_api_limit():
    """Check if we're approaching API limits"""
    global api_call_count
    api_call_count += 1
    if api_call_count > MAX_API_CALLS:
        print(f"⚠️ API call limit reached ({MAX_API_CALLS}), skipping further calls")
        return False
    return True


def get_female_performer_names(performers: List[dict]) -> str:
    """Get only female performer names (max 2)"""
    if not performers:
        return None
    
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
            if len(female_names) >= 2:  # Max 2 performers
                break
    
    if len(female_names) == 1:
        return female_names[0]
    elif len(female_names) == 2:
        return f"{female_names[0]} & {female_names[1]}"
    
    return None


def get_top_tags(tags: List[dict], max_tags: int = 5) -> List[str]:
    """Get top tags, cleaned and limited"""
    if not tags:
        return []
    
    formatted = []
    for tag in tags[:max_tags]:
        if isinstance(tag, dict):
            tag_name = tag.get('name', '')
        else:
            tag_name = str(tag)
        
        if tag_name:
            # Clean tag: lowercase, remove special chars
            clean = re.sub(r'[^\w\s]', '', tag_name.lower())
            clean = clean.replace(' ', '')
            if clean and len(clean) > 2:  # Skip short tags
                formatted.append(clean)
    
    return formatted[:max_tags]


def generate_clean_caption(scene_data: dict, original_filename: str = None, source: str = "local", performer: str = None, title: str = None, studio: str = None) -> str:
    """Generate sexy caption: Title + Female Performer(s) + Studio + Tags with separators
    NEW: Accepts detected studio from filename parsing"""
    
    # Use provided performer/title if available, otherwise extract from scene
    if title is None:
        title = scene_data.get('title', '')
    if not title and original_filename:
        _, title, _ = parse_filename_with_domain(original_filename, None)
    if not title:
        title = "Scene"
    
    # Clean title: remove brackets, years, extra spaces
    title = re.sub(r'[\[\]\(\)\{\}]', '', title)
    title = re.sub(r'\b(19|20)\d{2}\b', '', title)
    title = re.sub(r'\b\d{3,4}x\d{3,4}\b', '', title)
    title = re.sub(r'\s+', ' ', title).strip()
    
    # Get studio - use detected studio if provided, otherwise from scene data
    if studio is None:
        studio_obj = scene_data.get('studio', {})
        studio_name = studio_obj.get('name', '') if isinstance(studio_obj, dict) else ''
    else:
        studio_name = studio
    
    # Get female performer(s)
    if performer is None:
        performers = scene_data.get('performers', [])
        performer_str = get_female_performer_names(performers)
    else:
        performer_str = performer
    
    # Clean performer
    if performer_str:
        performer_str = re.sub(r'[\[\]\(\)\{\}]', '', performer_str)
        performer_str = re.sub(r'\s+', ' ', performer_str).strip()
    
    # Get top 5 tags
    tags = scene_data.get('tags', [])
    tag_list = get_top_tags(tags, max_tags=5)
    
    # Build sexy caption with separators (no emojis, clean style)
    lines = []
    
    # Source indicator
    source_emoji = "🌐" if source in ("stashdb", "fansdb") else "📁"
    
    # Title line with performer
    if performer_str and performer_str.lower() not in title.lower():
        lines.append(f"{source_emoji} *{performer_str} — {title}*")
    else:
        lines.append(f"{source_emoji} *{title}*")
    
    # Studio in monospace (no emoji)
    if studio_name:
        lines.append(f"`{studio_name}`")
    
    # Separator
    lines.append("━━━━━━━━━━━━━━")
    
    # Tags in monospace (no emoji)
    if tag_list:
        tags_str = ' '.join([f'`#{tag}`' for tag in tag_list])
        lines.append(f"{tags_str}")
    
    caption = '\n'.join(lines)
    
    # Truncate if too long
    if len(caption) > 1024:
        caption = caption[:1020] + "..."
    
    return caption


def find_performer_local_first(filename: str, parsed_performer: str = None) -> Tuple[Optional[str], Optional[str], float]:
    """
    Find performer using local database first, fallback to API.
    Returns: (performer_name, stashdb_id, confidence_score)
    """
    # First try local database
    if local_performer_search_available() and parsed_performer:
        print(f"🔍 Trying local DB for: '{parsed_performer}'")
        start_time = __import__('time').time()
        
        local_matches = search_performer_local(parsed_performer, limit=3, min_score=0.6)
        elapsed_ms = (__import__('time').time() - start_time) * 1000
        
        if local_matches:
            stashdb_id, name, score = local_matches[0]
            print(f"  ✓ Local DB match: '{name}' (score: {score:.0%}) in {elapsed_ms:.1f}ms")
            return name, stashdb_id, score
        else:
            print(f"  ✗ No local match found in {elapsed_ms:.1f}ms")
    
    # Return parsed performer if no local match
    return parsed_performer, None, 0.0


def _process_video_caption_impl(filename: str) -> Tuple[str, str, dict]:
    """
    Internal implementation: Search databases and return caption with full metadata.
    Tries each domain separately, picks the best match.
    For OnlyFans content, uses FansDB instead of StashDB.
    NEW: Enhanced matching with rapidfuzz, phonetic matching, and n-grams.
    Returns: (caption, source, metadata)
    """
    global api_call_count
    api_call_count = 0  # Reset counter
    
    # Check if this is OnlyFans content
    filename_lower = filename.lower()
    is_onlyfans = 'onlyfans' in filename_lower or 'of.' in filename_lower
    
    # Extract all domains from filename
    domains = extract_domains(filename)
    
    best_scene = None
    best_source = None
    best_match = 0.0
    best_domain = None
    best_performer = None
    best_title = None
    best_match_details = None
    
    # If no domains found, use the original parse
    if not domains:
        domains = [(None, filename)]  # (domain, clean_name) with no domain
    
    # Calculate minimum match threshold based on filename length
    # Short filenames need higher confidence
    base_name = re.sub(r'\.(mp4|mkv|avi|mov|wmv|flv|webm|m4v|mpeg|mpg)$', '', filename, flags=re.IGNORECASE)
    min_match_threshold = 0.50 if len(base_name) > 20 else 0.65  # Higher threshold for short names
    
    # For OnlyFans: ONLY use FansDB, skip StashDB
    if is_onlyfans:
        print(f"🔞 OnlyFans detected: {filename[:50]}")
        for domain, _ in domains:
            performer, title, detected_studio = parse_filename_with_domain(filename, domain)
            
            # Remove part numbers and clean
            title = re.sub(r'\bpart\s*\d+\b', '', title, flags=re.IGNORECASE)
            title = re.sub(r'\s+', ' ', title).strip()
            if performer:
                performer = re.sub(r'\bpart\s*\d+\b', '', performer, flags=re.IGNORECASE)
                performer = re.sub(r'\s+', ' ', performer).strip()
            
            # Use enhanced queries
            search_queries = build_enhanced_queries(filename, performer=performer, title=title)
            
            try:
                for query in search_queries[:3]:  # Limit to 3 queries
                    if not check_api_limit():
                        break
                    scene = search_fansdb(query, None)
                    if scene:
                        scene_title = scene.get('title', '')
                        performers = scene.get('performers', [])
                        scene_performer = ''
                        if performers and isinstance(performers[0], dict):
                            p = performers[0].get('performer', {})
                            if isinstance(p, dict):
                                scene_performer = p.get('name', '')
                        
                        # Use enhanced matching with phonetic support
                        title_sim, title_details = combined_similarity(title, scene_title, debug=True)
                        perf_sim = enhanced_performer_match(performer, scene_performer) if performer else 0.0
                        
                        # Calculate combined match
                        if title and performer:
                            combined_match = (title_sim * 0.6) + (perf_sim * 0.4)
                        elif title:
                            combined_match = title_sim
                        elif performer:
                            combined_match = perf_sim
                        else:
                            combined_match = 0.0
                        
                        # Debug logging
                        if DEBUG_MATCHING:
                            print(f"  FansDB '{query[:30]}...':")
                            print(f"    title={title_sim:.0%} ({title_details})")
                            print(f"    perf={perf_sim:.0%} (fuzzy+phonetic)")
                            print(f"    combined={combined_match:.0%}")
                        else:
                            print(f"  FansDB '{query[:30]}...': title={title_sim:.0%}, perf={perf_sim:.0%}, combined={combined_match:.0%}")
                        
                        # Apply minimum threshold for short names
                        if len(base_name) < 15 and combined_match < min_match_threshold:
                            print(f"    ↳ Skipped: below threshold {min_match_threshold:.0%}")
                            continue
                        
                        if combined_match > best_match:
                            best_match = combined_match
                            best_scene = scene
                            best_source = "fansdb"
                            best_domain = domain
                            best_performer = get_female_performer_names(performers)
                            best_title = scene_title
                            best_match_details = {
                                'title_sim': title_sim,
                                'perf_sim': perf_sim,
                                'algorithms': title_details if DEBUG_MATCHING else ['wratio', 'partial', 'token_sort', 'ngram'],
                                'phonetic_used': perf_sim > calculate_similarity(performer, scene_performer) if performer else False
                            }
                            
                        if combined_match >= 0.95:
                            break
            except Exception as e:
                print(f"FansDB error: {e}")
    
    else:
        # NON-OnlyFans: Try each domain with StashDB, pick best match
        # FIRST: Try local performer database for instant lookup
        local_performer_used = False
        if local_performer_search_available():
            # Parse first domain to get initial performer hint
            first_domain = domains[0][0] if domains else None
            parsed_perf, parsed_title, _ = parse_filename_with_domain(filename, first_domain)
            
            if parsed_perf:
                matched_name, stashdb_id, local_score = find_performer_local_first(filename, parsed_perf)
                if stashdb_id and local_score >= 0.75:
                    # High confidence local match - use it!
                    print(f"🚀 Using LOCAL DB match: '{matched_name}' (confidence: {local_score:.0%})")
                    local_performer_used = True
                    # Use this confirmed performer name for better API search
                    confirmed_performer = matched_name
                else:
                    confirmed_performer = parsed_perf
            else:
                confirmed_performer = None
        else:
            confirmed_performer = None
        
        for domain, _ in domains:
            performer, title, detected_studio = parse_filename_with_domain(filename, domain)
            
            # Use confirmed performer from local DB if available
            if confirmed_performer and not local_performer_used:
                # Local DB is available but no high-confidence match
                # Still use the parsed name
                pass
            elif confirmed_performer and local_performer_used:
                # Use the confirmed performer name for better matching
                performer = confirmed_performer
            
            # Remove part numbers and clean
            title = re.sub(r'\bpart\s*\d+\b', '', title, flags=re.IGNORECASE)
            title = re.sub(r'\s+', ' ', title).strip()
            if performer:
                performer = re.sub(r'\bpart\s*\d+\b', '', performer, flags=re.IGNORECASE)
                performer = re.sub(r'\s+', ' ', performer).strip()
            
            # Use enhanced queries
            search_queries = build_enhanced_queries(filename, performer=performer, title=title)
            
            # Try StashDB with this domain variation
            try:
                for query in search_queries[:3]:  # Limit to 3 queries
                    if not check_api_limit():
                        break
                    scene = search_stashdb(query, None)
                    if scene:
                        scene_title = scene.get('title', '')
                        performers = scene.get('performers', [])
                        scene_performer = ''
                        if performers and isinstance(performers[0], dict):
                            p = performers[0].get('performer', {})
                            if isinstance(p, dict):
                                scene_performer = p.get('name', '')
                        
                        # Use enhanced matching
                        title_sim, title_details = combined_similarity(title, scene_title, debug=True)
                        perf_sim = enhanced_performer_match(performer, scene_performer) if performer else 0.0
                        
                        # Calculate combined match
                        if title and performer:
                            combined_match = (title_sim * 0.6) + (perf_sim * 0.4)
                        elif title:
                            combined_match = title_sim
                        elif performer:
                            combined_match = perf_sim
                        else:
                            combined_match = 0.0
                        
                        # Debug logging
                        if DEBUG_MATCHING:
                            print(f"  StashDB '{query[:30]}...':")
                            print(f"    title={title_sim:.0%} ({title_details})")
                            print(f"    perf={perf_sim:.0%} (fuzzy+phonetic)")
                            print(f"    combined={combined_match:.0%}")
                        else:
                            print(f"  StashDB '{query[:30]}...': domain={domain}, title={title_sim:.0%}, perf={perf_sim:.0%}, combined={combined_match:.0%}")
                        
                        # Apply minimum threshold for short names
                        if len(base_name) < 15 and combined_match < min_match_threshold:
                            print(f"    ↳ Skipped: below threshold {min_match_threshold:.0%}")
                            continue
                        
                        if combined_match > best_match:
                            best_match = combined_match
                            best_scene = scene
                            best_source = "stashdb"
                            best_domain = domain
                            best_performer = get_female_performer_names(performers)
                            best_title = scene_title
                            best_match_details = {
                                'title_sim': title_sim,
                                'perf_sim': perf_sim,
                                'algorithms': title_details if DEBUG_MATCHING else ['wratio', 'partial', 'token_sort', 'ngram'],
                                'phonetic_used': perf_sim > calculate_similarity(performer, scene_performer) if performer else False
                            }
                            
                        if combined_match >= 0.95:
                            break
            except Exception as e:
                print(f"StashDB error: {e}")
        
        # If no good StashDB match, try FansDB as fallback (for non-OnlyFans too)
        if best_match < MATCH_THRESHOLD:
            for domain, _ in domains:
                performer, title, detected_studio = parse_filename_with_domain(filename, domain)
                
                title = re.sub(r'\bpart\s*\d+\b', '', title, flags=re.IGNORECASE)
                title = re.sub(r'\s+', ' ', title).strip()
                if performer:
                    performer = re.sub(r'\bpart\s*\d+\b', '', performer, flags=re.IGNORECASE)
                    performer = re.sub(r'\s+', ' ', performer).strip()
                
                search_queries = build_enhanced_queries(filename, performer=performer, title=title)
                
                try:
                    for query in search_queries[:3]:
                        if not check_api_limit():
                            break
                        scene = search_fansdb(query, None)
                        if scene:
                            scene_title = scene.get('title', '')
                            performers = scene.get('performers', [])
                            scene_performer = ''
                            if performers and isinstance(performers[0], dict):
                                p = performers[0].get('performer', {})
                                if isinstance(p, dict):
                                    scene_performer = p.get('name', '')
                            
                            title_sim, _ = combined_similarity(title, scene_title, debug=False)
                            perf_sim = enhanced_performer_match(performer, scene_performer) if performer else 0.0
                            combined_match = (title_sim * 0.6) + (perf_sim * 0.4) if (title and performer) else max(title_sim, perf_sim)
                            
                            print(f"  FansDB '{query[:30]}...': domain={domain}, title={title_sim:.0%}, perf={perf_sim:.0%}, combined={combined_match:.0%}")
                            
                            if combined_match > best_match:
                                best_match = combined_match
                                best_scene = scene
                                best_source = "fansdb"
                                best_domain = domain
                                best_performer = get_female_performer_names(performers)
                                best_title = scene_title
                                
                            if combined_match >= 0.95:
                                break
                except Exception as e:
                    print(f"FansDB error: {e}")
    
    # Return best match if good enough
    if best_scene and (best_match >= MATCH_THRESHOLD or MATCH_THRESHOLD == 0):
        if len(base_name) < 15 and best_match < min_match_threshold:
            print(f"⚠️ Best match {best_match:.0%} below min threshold {min_match_threshold:.0%} for short name, using local")
        else:
            # Use matched performer/title
            display_performer = best_performer
            display_title = best_title
            
            # Clean up display
            display_title = re.sub(r'\bpart\s*\d+\b', '', display_title, flags=re.IGNORECASE).strip() if display_title else ""
            if display_performer:
                display_performer = re.sub(r'\bpart\s*\d+\b', '', display_performer, flags=re.IGNORECASE).strip()
            
            # Log which algorithms contributed
            if best_match_details:
                algos = best_match_details.get('algorithms', [])
                phonetic = best_match_details.get('phonetic_used', False)
                algo_str = ', '.join(algos[:3]) if isinstance(algos, list) else 'wratio+partial+token'
                if phonetic:
                    algo_str += '+phonetic'
                
                if MATCH_THRESHOLD == 0 and best_match > 0:
                    print(f"✅ {best_source.upper()}: {filename[:50]} (match: {best_match:.0%}, algos: {algo_str}, no threshold)")
                else:
                    print(f"✅ {best_source.upper()}: {filename[:50]} (match: {best_match:.0%}, algos: {algo_str})")
            else:
                print(f"✅ {best_source.upper()}: {filename[:50]} (match: {best_match:.0%})")
            
            # Check if rename is suggested for high-confidence matches
            rename_suggestion = None
            should_offer_rename = should_rename(best_match, best_source)
            if should_offer_rename:
                rename_suggestion = get_rename_suggestion(best_scene, filename)
                print(f"📝 Rename suggested: {rename_suggestion['suggested']}")
            
            # Generate caption
            caption = generate_clean_caption(best_scene, filename, source=best_source, 
                                           performer=display_performer, title=display_title)
            
            # Store metadata for potential rename
            metadata = {
                'original_filename': filename,
                'match_score': best_match,
                'match_source': best_source,
                'scene_data': best_scene,
                'should_rename': should_offer_rename,
                'rename_suggestion': rename_suggestion
            }
            
            return caption, best_source, metadata
    
    # Fallback to local - use same format as StashDB matches
    print(f"📁 Local: {filename[:50]} (best match was {best_match:.0%}, need {MATCH_THRESHOLD:.0%})")
    
    # Detect studio from filename
    detected_studio, cleaned_name = detect_studio(filename)
    
    # Clean up title for display
    clean_title = re.sub(r'\b\w+\.(com|net|org|cc|co\.\w+|tv|xxx)\b', '', cleaned_name, flags=re.IGNORECASE)
    clean_title = re.sub(r'\.(mp4|mkv|avi|mov|wmv|flv|webm|m4v|mpeg|mpg)$', '', clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r'[\[\]\(\)\{\}]', '', clean_title)
    clean_title = re.sub(r'\b(19|20)\d{2}\b', '', clean_title)
    clean_title = re.sub(r'\b\d{3,4}x\d{3,4}\b', '', clean_title)
    clean_title = clean_title.replace('.', ' ').replace('_', ' ')
    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
    
    # Parse performer from cleaned title
    words = clean_title.split()
    performer_str = None
    title_str = clean_title
    
    if len(words) >= 2:
        # Try to detect studio (backup check)
        studios = ['brazzers', 'naughtyamerica', 'blacked', 'tushy', 'vixen', 'bangbros', 'realitykings']
        if words[0].lower() in studios and len(words) >= 3:
            performer_str = words[1]
            title_str = ' '.join(words[2:])
            if not detected_studio:
                detected_studio = words[0].title()
        else:
            # First word(s) as performer
            performer_str = words[0]
            title_str = ' '.join(words[1:])
    
    # Build caption in same format as StashDB (📁 for local instead of 🌐)
    lines = []
    if performer_str:
        lines.append(f"📁 *{performer_str} — {title_str}*")
    else:
        lines.append(f"📁 *{title_str}*")
    
    # Add detected studio in monospace
    if detected_studio:
        lines.append(f"`{detected_studio}`")
    
    # Separator
    lines.append("━━━━━━━━━━━━━━")
    
    caption = '\n'.join(lines)
    
    # Return metadata even for local matches (with should_rename=False)
    metadata = {
        'original_filename': filename,
        'match_score': best_match,
        'match_source': 'local',
        'scene_data': None,
        'should_rename': False,
        'rename_suggestion': None
    }
    
    return caption, "local", metadata


def process_video_caption(filename: str) -> Tuple[str, str]:
    """
    Backward-compatible wrapper that returns just caption and source.
    For full metadata, use process_video_caption_with_metadata().
    """
    caption, source, _ = _process_video_caption_impl(filename)
    return caption, source


def process_video_caption_with_metadata(filename: str) -> Tuple[str, str, dict]:
    """
    Enhanced version that returns full metadata including rename suggestions.
    
    Returns:
        Tuple of (caption: str, source: str, metadata: dict)
        metadata contains:
        - original_filename: str
        - match_score: float
        - match_source: str
        - scene_data: dict or None
        - should_rename: bool
        - rename_suggestion: dict or None
    """
    return _process_video_caption_impl(filename)


# Test
if __name__ == '__main__':
    test_files = [
        "Brazzers - J Mac - Big Tits At Work.mp4",
        "NaughtyAmerica - Caitlyn Smith - My Dads Hot Girlfriend.mp4",
        "Brazzers_BigTitsAtWork_JMac.mp4",
        "Riley Reid - Massage - Brazzers.mp4",
        "Jia Lissa - Midnight Ride.mp4",
        "Ariana Marie - Call Me.mp4",
    ]
    
    print("="*70)
    print("STASHDB INTEGRATION TEST - Enhanced Matching with Local DB")
    print("="*70)
    
    # Show local DB status
    if local_performer_search_available():
        print("✅ Local performer database is ACTIVE")
        db = get_performer_db()
        stats = db.get_stats()
        print(f"   📊 {stats.get('total_performers', 0):,} performers cached")
    else:
        print("⚠️  Local performer database not available")
        print("   Run: python update_performer_db.py")
    
    for filename in test_files:
        print(f"\n{'='*70}")
        print(f"File: {filename}")
        caption, source = process_video_caption(filename)
        if caption:
            print(f"Source: {source}")
            print(f"Caption:\n{caption}")
        else:
            print("No match found")