"""
Matching Utilities Module
Provides enhanced similarity algorithms for StashDB/FansDB matching
"""

import re
from typing import Dict, Optional, Tuple

# Try importing rapidfuzz, fallback to difflib if not available
try:
    from rapidfuzz import fuzz
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False
    import difflib

# Try importing jellyfish for phonetic matching
try:
    import jellyfish
    JELLYFISH_AVAILABLE = True
except ImportError:
    JELLYFISH_AVAILABLE = False

# Export availability flags for external use
__all__ = [
    'RAPIDFUZZ_AVAILABLE',
    'JELLYFISH_AVAILABLE',
    'calculate_similarity',
    'combined_similarity',
    'enhanced_performer_match',
    'calculate_match_confidence',
    'ngram_similarity',
    'phonetic_match',
    'normalize_single_char_name',
    'extract_performer_candidates',
    'split_concatenated_words',
    'handle_concatenated_title',
    'build_enhanced_queries',
]


# ============================================================
# N-GRAM SIMILARITY
# ============================================================

def ngram_similarity(str1: str, str2: str, n: int = 2) -> float:
    """
    Calculate n-gram similarity between two strings.
    Good for detecting substring matches and word variations.
    
    Args:
        str1: First string
        str2: Second string  
        n: N-gram size (2 = bigrams, 3 = trigrams)
    
    Returns:
        Similarity score from 0.0 to 1.0
    """
    if not str1 or not str2:
        return 0.0
    
    def get_ngrams(s: str, n: int) -> set:
        """Generate n-grams from a string"""
        # Remove spaces and convert to lowercase
        s = s.lower().replace(' ', '')
        if len(s) < n:
            return set()
        return set(s[i:i+n] for i in range(len(s) - n + 1))
    
    ngrams1 = get_ngrams(str1, n)
    ngrams2 = get_ngrams(str2, n)
    
    if not ngrams1 or not ngrams2:
        return 0.0
    
    # Jaccard similarity: intersection / union
    intersection = ngrams1 & ngrams2
    union = ngrams1 | ngrams2
    
    if not union:
        return 0.0
    
    return len(intersection) / len(union)


def bigram_similarity(str1: str, str2: str) -> float:
    """Convenience function for 2-gram similarity"""
    return ngram_similarity(str1, str2, n=2)


def trigram_similarity(str1: str, str2: str) -> float:
    """Convenience function for 3-gram similarity"""
    return ngram_similarity(str1, str2, n=3)


# ============================================================
# COMBINED SIMILARITY
# ============================================================

def combined_similarity(str1: str, str2: str, debug: bool = False):
    """
    Calculate combined similarity using multiple algorithms.
    Weighted combination of:
    - fuzz.WRatio (35%)
    - fuzz.partial_ratio (25%)
    - fuzz.token_sort_ratio (25%)
    - ngram_similarity n=2 (10%)
    - ngram_similarity n=3 (5%)
    
    Args:
        str1: First string (e.g., filename title)
        str2: Second string (e.g., DB title)
        debug: If True, returns (score, details_dict)
    
    Returns:
        Combined similarity score (float), or (score, details) if debug=True
    """
    if not str1 or not str2:
        if debug:
            return 0.0, {}
        return 0.0
    
    # Clean strings for comparison
    clean1 = re.sub(r'[^\w\s]', '', str1.lower()).strip()
    clean2 = re.sub(r'[^\w\s]', '', str2.lower()).strip()
    
    if not clean1 or not clean2:
        if debug:
            return 0.0, {}
        return 0.0
    
    # Calculate individual scores
    if RAPIDFUZZ_AVAILABLE:
        wratio = fuzz.WRatio(clean1, clean2) / 100.0
        partial = fuzz.partial_ratio(clean1, clean2) / 100.0
        token_sort = fuzz.token_sort_ratio(clean1, clean2) / 100.0
    else:
        # Fallback without rapidfuzz
        seq = difflib.SequenceMatcher(None, clean1, clean2)
        wratio = seq.ratio()
        partial = 1.0 if clean1 in clean2 or clean2 in clean1 else seq.ratio()
        token_sort = difflib.SequenceMatcher(None, sorted(clean1.split()), sorted(clean2.split())).ratio()
    
    # Add n-gram similarities
    ngram2 = ngram_similarity(clean1, clean2, n=2)
    ngram3 = ngram_similarity(clean1, clean2, n=3)
    
    scores = [wratio, partial, token_sort, ngram2, ngram3]
    
    # Weighted combination
    weights = [0.35, 0.25, 0.25, 0.10, 0.05]
    final_score = sum(s * w for s, w in zip(scores, weights))
    
    if debug:
        details = {
            'wratio': wratio,
            'partial': partial,
            'token_sort': token_sort,
            'ngram_2': ngram2,
            'ngram_3': ngram3,
            'final': final_score
        }
        return final_score, details
    
    return final_score


# ============================================================
# PHONETIC MATCHING
# ============================================================

def phonetic_match(str1: str, str2: str) -> float:
    """
    Check if two strings sound alike using phonetic algorithms.
    
    Args:
        str1: First string (e.g., filename performer)
        str2: Second string (e.g., DB performer)
    
    Returns:
        Phonetic similarity score from 0.0 to 1.0
    """
    if not str1 or not str2:
        return 0.0
    
    s1 = str1.lower().strip()
    s2 = str2.lower().strip()
    
    # Exact match
    if s1 == s2:
        return 1.0
    
    # Clean: remove special chars
    s1_clean = re.sub(r'[^\w]', '', s1)
    s2_clean = re.sub(r'[^\w]', '', s2)
    
    if s1_clean == s2_clean:
        return 1.0
    
    if not JELLYFISH_AVAILABLE:
        return 0.0
    
    # Soundex (for English names)
    try:
        soundex1 = jellyfish.soundex(s1_clean)
        soundex2 = jellyfish.soundex(s2_clean)
        if soundex1 == soundex2 and soundex1 != '0000':
            return 0.85  # High confidence phonetic match
    except Exception:
        pass
    
    # Metaphone (improved Soundex)
    try:
        meta1 = jellyfish.metaphone(s1_clean)
        meta2 = jellyfish.metaphone(s2_clean)
        if meta1 and meta2 and meta1 == meta2:
            return 0.90  # Very high confidence
    except Exception:
        pass
    
    # NYSIIS (better for non-English names)
    try:
        nysiis1 = jellyfish.nysiis(s1_clean)
        nysiis2 = jellyfish.nysiis(s2_clean)
        if nysiis1 == nysiis2:
            return 0.85
    except Exception:
        pass
    
    return 0.0


def enhanced_performer_match(filename_performer: str, db_performer: str, filename: str = None) -> float:
    """
    Combined string + phonetic matching for performer names.
    Checks learned corrections FIRST for filename-specific patterns.
    
    Args:
        filename_performer: Performer name from filename
        db_performer: Performer name from database
        filename: Original filename (optional, for pattern matching)
    
    Returns:
        Best similarity score from 0.0 to 1.0
    """
    if not filename_performer or not db_performer:
        return 0.0
    
    # STEP 1: Check learned corrections FIRST (user correction > algorithm guess)
    try:
        from feedback_db import apply_learned_corrections
        if filename:
            corrected_performer, confidence = apply_learned_corrections(filename, filename_performer)
            if corrected_performer != filename_performer:
                # User has corrected this before - check if it matches db_performer
                if RAPIDFUZZ_AVAILABLE:
                    learned_match = fuzz.WRatio(corrected_performer, db_performer) / 100.0
                else:
                    clean1 = re.sub(r'[^\w\s]', '', corrected_performer.lower()).strip()
                    clean2 = re.sub(r'[^\w\s]', '', db_performer.lower()).strip()
                    learned_match = difflib.SequenceMatcher(None, clean1, clean2).ratio()
                
                # Boost score based on confidence and learned correction
                if learned_match > 0.7:
                    # High match with learned correction - boost significantly
                    boosted_score = min(1.0, learned_match * (1 + confidence * 0.2))
                    return boosted_score
    except Exception as e:
        # If feedback_db not available or error, continue with normal matching
        pass
    
    # STEP 2: Standard matching algorithms
    # Try rapidfuzz weighted ratio
    if RAPIDFUZZ_AVAILABLE:
        fuzzy_score = fuzz.WRatio(filename_performer, db_performer) / 100.0
        # Also try partial ratio (good for short names like "J Mac" vs "J. Mac")
        partial_score = fuzz.partial_ratio(filename_performer, db_performer) / 100.0
    else:
        clean1 = re.sub(r'[^\w\s]', '', filename_performer.lower()).strip()
        clean2 = re.sub(r'[^\w\s]', '', db_performer.lower()).strip()
        fuzzy_score = difflib.SequenceMatcher(None, clean1, clean2).ratio()
        partial_score = 1.0 if clean1 in clean2 or clean2 in clean1 else fuzzy_score
    
    # Try phonetic match
    phonetic_score = phonetic_match(filename_performer, db_performer)
    
    # Return best of all methods
    return max(fuzzy_score, phonetic_score, partial_score)


# ============================================================
# MATCH CONFIDENCE SCORING
# ============================================================

def calculate_match_confidence(
    filename: str,
    scene_data: dict,
    parsed_performer: str = None,
    parsed_title: str = None,
    detected_studio: str = None
) -> Dict[str, float]:
    """
    Calculate comprehensive match confidence score.
    
    Args:
        filename: Original filename
        scene_data: Scene data from StashDB/FansDB
        parsed_performer: Performer name extracted from filename
        parsed_title: Title extracted from filename
        detected_studio: Studio name detected from filename
    
    Returns:
        Dict with individual scores and final confidence:
        - title_similarity: 0.0 to 1.0 (using combined_similarity)
        - performer_similarity: 0.0 to 1.0 (using enhanced_performer_match)
        - studio_match: 0.0 or 1.0 (bonus if studios align)
        - final_score: Weighted combination (0.0 to 1.0)
        - strong_match_bonus: True if both performer and title match well
    """
    # Extract data from scene
    scene_title = scene_data.get('title', '') or ''
    scene_studio = ''
    
    studio_obj = scene_data.get('studio', {})
    if isinstance(studio_obj, dict):
        scene_studio = studio_obj.get('name', '') or ''
    
    # Get performer from scene
    performers = scene_data.get('performers', [])
    scene_performer = ''
    if performers and isinstance(performers[0], dict):
        p = performers[0].get('performer', {})
        if isinstance(p, dict):
            scene_performer = p.get('name', '') or ''
    
    scores = {
        'title_similarity': 0.0,
        'performer_similarity': 0.0,
        'studio_match': 0.0,
        'final_score': 0.0,
        'strong_match_bonus': False
    }
    
    # 1. Title similarity (using combined algorithm)
    if parsed_title and scene_title:
        scores['title_similarity'] = combined_similarity(parsed_title, scene_title)
    
    # 2. Performer similarity (with learned corrections + phonetic matching)
    if parsed_performer and scene_performer:
        scores['performer_similarity'] = enhanced_performer_match(
            parsed_performer, scene_performer, filename
        )
    
    # 3. Studio match (bonus points)
    if detected_studio and scene_studio:
        if RAPIDFUZZ_AVAILABLE:
            studio_score = fuzz.WRatio(detected_studio, scene_studio) / 100.0
        else:
            studio_score = 1.0 if detected_studio.lower() == scene_studio.lower() else 0.0
        scores['studio_match'] = 1.0 if studio_score > 0.8 else 0.0
    
    # Calculate final weighted score
    weights = {
        'title_similarity': 0.45,
        'performer_similarity': 0.40,
        'studio_match': 0.15,
    }
    
    # Only include scores that have valid values
    total_weight = 0.0
    weighted_sum = 0.0
    
    for key, weight in weights.items():
        if scores[key] > 0:
            weighted_sum += scores[key] * weight
            total_weight += weight
    
    if total_weight > 0:
        final = weighted_sum / total_weight  # Normalize by actual weights used
    else:
        final = 0.0
    
    # 10% boost if both performer AND title match well
    if scores['performer_similarity'] > 0.8 and scores['title_similarity'] > 0.7:
        final = min(1.0, final * 1.1)  # 10% boost, cap at 1.0
        scores['strong_match_bonus'] = True
    
    scores['final_score'] = final
    return scores


# ============================================================
# LEGACY COMPATIBILITY
# ============================================================

def calculate_similarity(str1: str, str2: str) -> float:
    """
    Legacy compatibility: Calculate similarity using rapidfuzz.WRatio.
    This replaces the old difflib.SequenceMatcher approach.
    """
    if not str1 or not str2:
        return 0.0
    
    # Clean strings: lowercase, remove special chars
    clean1 = re.sub(r'[^\w\s]', '', str1.lower()).strip()
    clean2 = re.sub(r'[^\w\s]', '', str2.lower()).strip()
    
    if not clean1 or not clean2:
        return 0.0
    
    if RAPIDFUZZ_AVAILABLE:
        return fuzz.WRatio(clean1, clean2) / 100.0
    else:
        return difflib.SequenceMatcher(None, clean1, clean2).ratio()


# ============================================================
# SINGLE-CHARACTER PERFORMER HANDLING
# ============================================================

def normalize_single_char_name(name: str) -> str:
    """
    Normalize names with single-letter first names.
    Converts "JMac" to "J Mac", "J.Mac" to "J Mac", etc.
    
    Args:
        name: The name to normalize
        
    Returns:
        Normalized name with spaces between single letter and rest
    """
    if not name:
        return name
    
    # Pattern: Single letter + optional punctuation + capitalized word
    # Matches: JMac, J.Mac, J-Mac, O'Connor, A.J. Applegate
    # Replace with "J Mac", "O Connor", "A J Applegate"
    normalized = re.sub(r'\b([A-Za-z])[\.\-_\']?\s*([A-Z][a-z]+)\b', r'\1 \2', name)
    
    return normalized


def extract_performer_candidates(filename: str) -> list:
    """
    Extract potential performer names from filename.
    Handles single-letter first names like "J Mac", "O Mariah".
    
    Args:
        filename: The filename to parse
        
    Returns:
        List of potential performer name candidates
    """
    # Remove extension and brackets
    name = re.sub(r'\.(mp4|mkv|avi|mov|wmv|flv|webm|m4v|mpeg|mpg)$', '', 
                  filename, flags=re.IGNORECASE)
    name = re.sub(r'[\[\]\(\)\{\}]', '', name)
    
    candidates = []
    
    # Pattern 1: "Studio - Performer - Title" format
    parts = name.split(' - ')
    if len(parts) >= 2:
        # Second part is often the performer
        candidates.append(parts[1].strip())
        
        # Also try joined version
        joined = parts[1].strip().replace(' ', '')
        if joined != parts[1].strip():
            candidates.append(joined)
    
    # Pattern 2: First 2-3 words could be "First Last"
    words = re.split(r'[.\-_ ]+', name)
    clean_words = [w for w in words if len(w) >= 1]
    
    if len(clean_words) >= 2:
        candidates.append(f"{clean_words[0]} {clean_words[1]}")
        # Check for single letter first name
        if len(clean_words[0]) == 1:
            candidates.append(f"{clean_words[0]}{clean_words[1]}")
    
    if len(clean_words) >= 3:
        candidates.append(f"{clean_words[0]} {clean_words[1]} {clean_words[2]}")
    
    return list(set(c.strip() for c in candidates if len(c.strip()) > 1))


def split_concatenated_words(text: str) -> list:
    """
    Split concatenated words like "BigTitsAtWork" into separate words.
    Uses camelCase/PascalCase detection.
    
    Args:
        text: Text with potentially concatenated words
        
    Returns:
        List of split words
    """
    if not text:
        return []
    
    # Insert space before uppercase letters that follow lowercase
    s1 = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', text)
    # Also handle consecutive capitals (e.g., "URLParser" -> "URL Parser")
    s2 = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', s1)
    
    return s2.split()


def handle_concatenated_title(title: str) -> str:
    """
    Handle titles with concatenated words like "BigTitsAtWork".
    
    Args:
        title: Title that might have concatenated words
        
    Returns:
        Title with words separated
    """
    if not title:
        return title
    
    words = split_concatenated_words(title)
    if words:
        return ' '.join(words)
    return title


def build_enhanced_queries(filename: str, performer: str = None, title: str = None) -> list:
    """
    Build multiple search query variations for better matching.
    
    Args:
        filename: The original filename
        performer: Extracted performer name (optional)
        title: Extracted title (optional)
        
    Returns:
        List of search query strings
    """
    queries = []
    
    # Get path words from filename
    # Remove extension
    clean_filename = re.sub(r'\.(mp4|mkv|avi|mov|wmv|flv|webm|m4v|mpeg|mpg)$', '', 
                            filename, flags=re.IGNORECASE)
    
    # Build word list from filename
    words = re.split(r'[.\-_ ]+', clean_filename)
    stop_words = {'com', 'net', 'org', 'www', 'part', 'mp4', 'mkv', 'avi', 
                  'the', 'and', 'of', 'in', 'hd', 'sd', '4k', '1080p', '720p',
                  'xxx', 'porn', 'video', 'clip'}
    clean_words = [w for w in words if len(w) >= 2 and w.lower() not in stop_words]
    
    # Query 1: All significant words (up to 6)
    if clean_words:
        queries.append(' '.join(clean_words[:6]))
    
    # Query 2: Performer-focused
    if performer:
        queries.append(performer)
        # Wildcard for short names (Stash style)
        if len(performer) < 15:
            queries.append(f"*{performer}*")
    
    # Query 3: Title only
    if title:
        queries.append(title)
        # Handle concatenated title
        split_title = handle_concatenated_title(title)
        if split_title != title:
            queries.append(split_title)
    
    # Query 4: Performer + Title
    if performer and title:
        queries.append(f"{performer} {title}")
    
    # Query 5: First-2-chars fingerprint (for fuzzy matching)
    if len(clean_words) >= 2:
        f2_pattern = ' '.join([w[:2] if len(w) > 2 else w for w in clean_words[:5]])
        queries.append(f2_pattern)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_queries = []
    for q in queries:
        if q and q not in seen:
            seen.add(q)
            unique_queries.append(q)
    
    return unique_queries
