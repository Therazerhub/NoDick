"""
Performer Database Module
Local SQLite-based performer database for instant lookups (0ms vs 2s API calls)
Enhanced with fuzzy + phonetic matching and StashDB API integration
"""

import os
import re
import sqlite3
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple, Any
from dataclasses import dataclass
from pathlib import Path

import requests

# Use NoDick config
from nodick.config import settings

# Import matching utilities for fuzzy search
try:
    from nodick.metadata.matching import (
        calculate_similarity,
        combined_similarity,
        enhanced_performer_match,
        phonetic_match,
        RAPIDFUZZ_AVAILABLE,
        JELLYFISH_AVAILABLE
    )
except ImportError:
    # Fallback if matching not available
    RAPIDFUZZ_AVAILABLE = False
    JELLYFISH_AVAILABLE = False
    
    def calculate_similarity(s1, s2):
        if not s1 or not s2:
            return 0.0
        # Simple ratio
        s1, s2 = s1.lower(), s2.lower()
        if s1 == s2:
            return 1.0
        return len(set(s1) & set(s2)) / len(set(s1) | set(s2))
    
    def combined_similarity(s1, s2, debug=False):
        score = calculate_similarity(s1, s2)
        return (score, {}) if debug else score
    
    def enhanced_performer_match(s1, s2):
        return calculate_similarity(s1, s2)
    
    def phonetic_match(s1, s2):
        return 0.0


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
PERFORMER_DB_PATH = os.getenv('PERFORMER_DB_PATH', str(settings.root / 'runtime' / 'performers.db'))
STALE_DAYS = 7  # Warn if DB is older than this

# StashDB API configuration — use NoDick settings
STASHDB_API_KEY = settings.stashdb_api_key
STASHDB_GRAPHQL_URL = settings.stashdb_graphql_url

# GraphQL query to fetch all performers with their details
ALL_PERFORMERS_QUERY = """
query QueryPerformers($input: PerformerQueryInput!) {
  queryPerformers(input: $input) {
    performers {
      id
      name
      gender
      birth_date
      ethnicity
      country
      eye_color
      hair_color
      height
      weight
      measurements
      career_start_year
      career_end_year
      breast_type
      aliases {
        alias
      }
      images {
        url
        default
      }
      created_at
      updated_at
    }
    count
  }
}
"""


@dataclass
class Performer:
    """Performer data structure matching StashDB schema"""
    id: str  # StashDB UUID
    name: str
    aliases: List[str]
    gender: Optional[str] = None
    birthdate: Optional[str] = None
    ethnicity: Optional[str] = None
    country: Optional[str] = None
    eye_color: Optional[str] = None
    hair_color: Optional[str] = None
    height: Optional[int] = None  # cm
    weight: Optional[int] = None  # kg
    measurements: Optional[str] = None
    career_start_year: Optional[int] = None
    career_end_year: Optional[int] = None
    breast_type: Optional[str] = None
    images: List[Dict[str, Any]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_synced: Optional[str] = None
    
    def __post_init__(self):
        if self.aliases is None:
            self.aliases = []
        if self.images is None:
            self.images = []
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'aliases': self.aliases,
            'gender': self.gender,
            'birthdate': self.birthdate,
            'ethnicity': self.ethnicity,
            'country': self.country,
            'eye_color': self.eye_color,
            'hair_color': self.hair_color,
            'height': self.height,
            'weight': self.weight,
            'measurements': self.measurements,
            'career_start_year': self.career_start_year,
            'career_end_year': self.career_end_year,
            'breast_type': self.breast_type,
            'images': self.images,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'last_synced': self.last_synced
        }


class PerformerDB:
    """
    Local performer database with instant search capabilities.
    Mirrors StashDB performer data locally for fast lookups.
    """
    
    def __init__(self, db_path: str = None):
        self.db_path = db_path or PERFORMER_DB_PATH
        self._conn: Optional[sqlite3.Connection] = None
        self._cursor: Optional[sqlite3.Cursor] = None
    
    def _connect(self):
        """Connect to the SQLite database"""
        if self._conn is None:
            # Ensure parent dir exists (Render image has no runtime/ dir)
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
            self._cursor = self._conn.cursor()
            # Ensure schema exists — init_db() is idempotent (CREATE TABLE IF NOT
            # EXISTS) and safe here: it calls _connect() again, which returns early
            # since the connection is already set. Without this, every lookup fails
            # with "no such table: performers" on a fresh/empty DB file.
            self.init_db()
    
    def _close(self):
        """Close the database connection"""
        if self._conn:
            self._conn.close()
            self._conn = None
            self._cursor = None
    
    def __enter__(self):
        """Context manager entry"""
        self._connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self._close()
        return False
    
    def init_db(self) -> None:
        """
        Initialize the performer database tables.
        Creates tables for performers, aliases, images, and metadata.
        """
        self._connect()
        
        # Main performers table
        self._cursor.execute('''
            CREATE TABLE IF NOT EXISTS performers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                gender TEXT,
                birthdate TEXT,
                ethnicity TEXT,
                country TEXT,
                eye_color TEXT,
                hair_color TEXT,
                height INTEGER,
                weight INTEGER,
                measurements TEXT,
                career_start_year INTEGER,
                career_end_year INTEGER,
                breast_type TEXT,
                created_at TEXT,
                updated_at TEXT,
                last_synced TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Aliases table (one-to-many relationship)
        self._cursor.execute('''
            CREATE TABLE IF NOT EXISTS aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                performer_id TEXT NOT NULL,
                alias_name TEXT NOT NULL,
                FOREIGN KEY (performer_id) REFERENCES performers(id) ON DELETE CASCADE,
                UNIQUE(performer_id, alias_name)
            )
        ''')
        
        # Images table (one-to-many relationship)
        self._cursor.execute('''
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                performer_id TEXT NOT NULL,
                image_url TEXT NOT NULL,
                is_default BOOLEAN DEFAULT FALSE,
                FOREIGN KEY (performer_id) REFERENCES performers(id) ON DELETE CASCADE
            )
        ''')
        
        # Search index for faster lookups
        self._cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_performer_name ON performers(name)
        ''')
        
        self._cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_alias_name ON aliases(alias_name)
        ''')
        
        # Metadata table for tracking sync status
        self._cursor.execute('''
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        self._conn.commit()
        logger.info(f"Database initialized at {self.db_path}")
    
    def _init_db(self) -> None:
        """Backward compatibility wrapper for init_db"""
        self.init_db()
    
    def fetch_all_performers_from_stashdb(self, batch_size: int = 100) -> List[Dict[str, Any]]:
        """
        Fetch all performers from StashDB API using pagination.
        
        Args:
            batch_size: Number of performers to fetch per request
            
        Returns:
            List of performer dictionaries from StashDB
        """
        if not STASHDB_API_KEY:
            raise ValueError("STASHDB_API_KEY not found in environment variables")
        
        all_performers = []
        current_page = 1
        has_more = True
        
        headers = {
            'Content-Type': 'application/json',
            'ApiKey': STASHDB_API_KEY
        }
        
        logger.info("Starting to fetch performers from StashDB...")
        
        while has_more:
            variables = {
                'input': {
                    'page': current_page,
                    'per_page': batch_size,
                    'sort': 'name',
                    'direction': 'ASC'
                }
            }
            
            payload = {
                'query': ALL_PERFORMERS_QUERY,
                'variables': variables
            }
            
            try:
                response = requests.post(
                    STASHDB_GRAPHQL_URL,
                    json=payload,
                    headers=headers,
                    timeout=60
                )
                response.raise_for_status()
                data = response.json()
                
                if 'errors' in data:
                    logger.error(f"GraphQL errors: {data['errors']}")
                    break
                
                result = data.get('data', {}).get('queryPerformers', {})
                performers = result.get('performers', [])
                total_count = result.get('count', 0)
                
                if not performers:
                    has_more = False
                    break
                
                all_performers.extend(performers)
                logger.info(f"Fetched page {current_page}: {len(performers)} performers (total: {len(all_performers)}/{total_count})")
                
                # Check if we've fetched all performers
                if len(performers) < batch_size or len(all_performers) >= total_count:
                    has_more = False
                else:
                    current_page += 1
                    
            except requests.exceptions.RequestException as e:
                logger.error(f"API request failed: {e}")
                break
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                break
        
        logger.info(f"Total performers fetched: {len(all_performers)}")
        return all_performers
    
    def update_local_db(self, performers: Optional[List[Dict[str, Any]]] = None) -> None:
        """
        Populate/update the local database with performers from StashDB.
        
        Args:
            performers: List of performers from StashDB. If None, will fetch from API.
        """
        self._connect()
        
        if performers is None:
            performers = self.fetch_all_performers_from_stashdb()
        
        logger.info(f"Updating local database with {len(performers)} performers...")
        
        updated_count = 0
        new_count = 0
        
        for performer_data in performers:
            try:
                stashdb_id = performer_data.get('id')
                name = performer_data.get('name', '')
                
                # Check if performer already exists
                self._cursor.execute(
                    'SELECT id FROM performers WHERE id = ?',
                    (stashdb_id,)
                )
                existing = self._cursor.fetchone()
                
                if existing:
                    # Update existing performer
                    self._cursor.execute('''
                        UPDATE performers SET
                            name = ?,
                            gender = ?,
                            birthdate = ?,
                            ethnicity = ?,
                            country = ?,
                            eye_color = ?,
                            hair_color = ?,
                            height = ?,
                            weight = ?,
                            measurements = ?,
                            career_start_year = ?,
                            career_end_year = ?,
                            breast_type = ?,
                            updated_at = ?,
                            last_synced = CURRENT_TIMESTAMP
                        WHERE id = ?
                    ''', (
                        name,
                        performer_data.get('gender'),
                        performer_data.get('birth_date'),
                        performer_data.get('ethnicity'),
                        performer_data.get('country'),
                        performer_data.get('eye_color'),
                        performer_data.get('hair_color'),
                        performer_data.get('height'),
                        performer_data.get('weight'),
                        performer_data.get('measurements'),
                        performer_data.get('career_start_year'),
                        performer_data.get('career_end_year'),
                        performer_data.get('breast_type'),
                        performer_data.get('updated_at'),
                        stashdb_id
                    ))
                    updated_count += 1
                    
                    # Delete old aliases and images (will re-insert)
                    self._cursor.execute('DELETE FROM aliases WHERE performer_id = ?', (stashdb_id,))
                    self._cursor.execute('DELETE FROM images WHERE performer_id = ?', (stashdb_id,))
                else:
                    # Insert new performer
                    self._cursor.execute('''
                        INSERT INTO performers (
                            id, name, gender, birthdate, ethnicity,
                            country, eye_color, hair_color, height, weight,
                            measurements, career_start_year, career_end_year,
                            breast_type, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        stashdb_id,
                        name,
                        performer_data.get('gender'),
                        performer_data.get('birth_date'),
                        performer_data.get('ethnicity'),
                        performer_data.get('country'),
                        performer_data.get('eye_color'),
                        performer_data.get('hair_color'),
                        performer_data.get('height'),
                        performer_data.get('weight'),
                        performer_data.get('measurements'),
                        performer_data.get('career_start_year'),
                        performer_data.get('career_end_year'),
                        performer_data.get('breast_type'),
                        performer_data.get('created_at'),
                        performer_data.get('updated_at')
                    ))
                    new_count += 1
                
                # Insert aliases
                aliases = performer_data.get('aliases', [])
                for alias_data in aliases:
                    alias_name = alias_data.get('alias', '') if isinstance(alias_data, dict) else str(alias_data)
                    if alias_name:
                        self._cursor.execute('''
                            INSERT OR IGNORE INTO aliases (performer_id, alias_name)
                            VALUES (?, ?)
                        ''', (stashdb_id, alias_name))
                
                # Insert images
                images = performer_data.get('images', [])
                for image_data in images:
                    if isinstance(image_data, dict):
                        image_url = image_data.get('url', '')
                        is_default = image_data.get('default', False)
                    else:
                        image_url = str(image_data)
                        is_default = False
                    
                    if image_url:
                        self._cursor.execute('''
                            INSERT INTO images (performer_id, image_url, is_default)
                            VALUES (?, ?, ?)
                        ''', (stashdb_id, image_url, is_default))
                
            except Exception as e:
                logger.error(f"Error processing performer {name if 'name' in locals() else 'unknown'}: {e}")
                continue
        
        # Update metadata
        self._cursor.execute('''
            INSERT OR REPLACE INTO metadata (key, value, updated_at)
            VALUES ('last_sync', ?, CURRENT_TIMESTAMP)
        ''', (datetime.now().isoformat(),))
        
        self._conn.commit()
        logger.info(f"Database update complete: {new_count} new, {updated_count} updated")
    
    def search_performer(self, name: str, limit: int = 10, min_score: float = 0.3) -> List[Tuple[Performer, float]]:
        """
        Search for performers with fuzzy + phonetic matching.
        
        Args:
            name: The search name/query
            limit: Maximum number of results to return
            min_score: Minimum similarity score (0.0 to 1.0)
            
        Returns:
            List of tuples (Performer, score) sorted by score descending
        """
        self._connect()
        
        search_name = name.lower().strip()
        
        # Get all performers with their aliases
        self._cursor.execute('''
            SELECT p.*, GROUP_CONCAT(a.alias_name) as aliases_str
            FROM performers p
            LEFT JOIN aliases a ON p.id = a.performer_id
            GROUP BY p.id
        ''')
        
        rows = self._cursor.fetchall()
        
        matches = []
        
        for row in rows:
            performer_name = row['name'].lower()
            aliases_str = row['aliases_str'] or ''
            aliases = [a.strip() for a in aliases_str.split(',') if a.strip()]
            
            scores = []
            
            # Score against main name
            name_score = combined_similarity(search_name, performer_name)
            scores.append(name_score)
            
            # Score against aliases
            for alias in aliases:
                alias_score = combined_similarity(search_name, alias.lower())
                scores.append(alias_score)
            
            # Phonetic matching
            phonetic_score = 0.0
            if JELLYFISH_AVAILABLE:
                phonetic_score = phonetic_match(search_name, performer_name)
                for alias in aliases:
                    alias_phonetic = phonetic_match(search_name, alias.lower())
                    phonetic_score = max(phonetic_score, alias_phonetic)
            
            # Use best score
            best_score = max(scores + [phonetic_score * 0.9])
            
            if best_score >= min_score:
                performer = self._row_to_performer(row)
                matches.append((performer, best_score))
        
        # Sort by score descending and limit
        matches.sort(key=lambda x: x[1], reverse=True)
        return matches[:limit]
    
    def get_performer_by_id(self, stashdb_id: str) -> Optional[Performer]:
        """
        Instant lookup by StashDB ID.
        
        Args:
            stashdb_id: The StashDB performer ID
            
        Returns:
            Performer object if found, None otherwise
        """
        self._connect()
        
        self._cursor.execute('SELECT * FROM performers WHERE id = ?', (stashdb_id,))
        row = self._cursor.fetchone()
        
        if row:
            return self._row_to_performer(row)
        return None
    
    def _row_to_performer(self, row: sqlite3.Row) -> Performer:
        """Convert a database row to a Performer object"""
        performer_id = row['id']
        
        # Fetch aliases
        self._cursor.execute(
            'SELECT alias_name FROM aliases WHERE performer_id = ?',
            (performer_id,)
        )
        aliases = [r['alias_name'] for r in self._cursor.fetchall()]
        
        # Fetch images
        self._cursor.execute(
            'SELECT image_url, is_default FROM images WHERE performer_id = ?',
            (performer_id,)
        )
        images = [
            {'url': r['image_url'], 'default': bool(r['is_default'])}
            for r in self._cursor.fetchall()
        ]
        
        return Performer(
            id=performer_id,
            name=row['name'],
            gender=row['gender'],
            birthdate=row['birthdate'],
            ethnicity=row['ethnicity'],
            country=row['country'],
            eye_color=row['eye_color'],
            hair_color=row['hair_color'],
            height=row['height'],
            weight=row['weight'],
            measurements=row['measurements'],
            career_start_year=row['career_start_year'],
            career_end_year=row['career_end_year'],
            breast_type=row['breast_type'],
            aliases=aliases,
            images=images,
            created_at=row['created_at'],
            updated_at=row['updated_at'],
            last_synced=row['last_synced']
        )
    
    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics"""
        self._connect()
        
        stats = {}
        
        # Count performers
        self._cursor.execute('SELECT COUNT(*) as count FROM performers')
        stats['total_performers'] = self._cursor.fetchone()['count']
        
        # Count aliases
        self._cursor.execute('SELECT COUNT(*) as count FROM aliases')
        stats['total_aliases'] = self._cursor.fetchone()['count']
        
        # Count images
        self._cursor.execute('SELECT COUNT(*) as count FROM images')
        stats['total_images'] = self._cursor.fetchone()['count']
        
        # Last sync time
        self._cursor.execute("SELECT value FROM metadata WHERE key = 'last_sync'")
        row = self._cursor.fetchone()
        stats['last_sync'] = row['value'] if row else None
        
        # Database file size
        db_path = Path(self.db_path)
        if db_path.exists():
            stats['db_size_mb'] = round(db_path.stat().st_size / (1024 * 1024), 2)
        
        # Check if stale
        last_sync = stats.get('last_sync')
        if last_sync:
            try:
                sync_time = datetime.fromisoformat(last_sync)
                stats['is_stale'] = (datetime.now() - sync_time) > timedelta(days=STALE_DAYS)
            except:
                stats['is_stale'] = True
        else:
            stats['is_stale'] = True
        
        return stats
    
    # Backward compatibility methods
    
    def get_last_update(self) -> Optional[datetime]:
        """Get the last time the database was updated"""
        stats = self.get_stats()
        last_sync = stats.get('last_sync')
        if last_sync:
            try:
                return datetime.fromisoformat(last_sync)
            except:
                pass
        return None
    
    def is_stale(self, days: int = STALE_DAYS) -> bool:
        """Check if the database is stale"""
        stats = self.get_stats()
        return stats.get('is_stale', True)
    
    def add_performer(self, performer: Performer) -> bool:
        """Add or update a performer in the database (backward compat)"""
        try:
            self.update_local_db([{
                'id': performer.id,
                'name': performer.name,
                'gender': performer.gender,
                'birth_date': performer.birthdate,
                'ethnicity': performer.ethnicity,
                'country': performer.country,
                'eye_color': performer.eye_color,
                'hair_color': performer.hair_color,
                'height': performer.height,
                'weight': performer.weight,
                'measurements': performer.measurements,
                'career_start_year': performer.career_start_year,
                'career_end_year': performer.career_end_year,
                'aliases': [{'alias': a} for a in performer.aliases],
                'images': performer.images,
                'created_at': performer.created_at,
                'updated_at': performer.updated_at
            }])
            return True
        except Exception as e:
            logger.error(f"Error adding performer: {e}")
            return False
    
    def search_performers(self, query: str, limit: int = 10, gender: str = None) -> List[Performer]:
        """Search performers (backward compat, returns Performer objects only)"""
        results = self.search_performer(query, limit=limit, min_score=0.2)
        
        if gender:
            results = [(p, s) for p, s in results if p.gender == gender]
        
        return [p for p, _ in results]
    
    def get_performer_by_name(self, name: str) -> Optional[Performer]:
        """Get performer by exact name match"""
        self._connect()
        self._cursor.execute(
            'SELECT * FROM performers WHERE LOWER(name) = LOWER(?) LIMIT 1',
            (name,)
        )
        row = self._cursor.fetchone()
        if row:
            return self._row_to_performer(row)
        return None
    
    def update_last_update_time(self) -> None:
        """Update the last sync timestamp"""
        self._connect()
        self._cursor.execute('''
            INSERT OR REPLACE INTO metadata (key, value, updated_at)
            VALUES ('last_sync', ?, CURRENT_TIMESTAMP)
        ''', (datetime.now().isoformat(),))
        self._conn.commit()


# Global instance for convenience
_performer_db = None


def get_performer_db() -> PerformerDB:
    """Get or create the global performer database instance"""
    global _performer_db
    if _performer_db is None:
        _performer_db = PerformerDB()
    return _performer_db


def init_database(db_path: str = None):
    """Initialize the database (create tables) - convenience function"""
    db = PerformerDB(db_path)
    db.init_db()
    db._close()


def search_performers_fast(query: str, limit: int = 10) -> List[Performer]:
    """Convenience function for quick performer search"""
    db = get_performer_db()
    return db.search_performers(query, limit=limit, gender='FEMALE')


def local_performer_search_available() -> bool:
    """Check if local performer database is available and populated"""
    try:
        db = get_performer_db()
        stats = db.get_stats()
        return stats.get('total_performers', 0) > 0
    except Exception:
        return False


def check_performer_db() -> Tuple[bool, Dict[str, Any]]:
    """
    Check performer database status at startup.
    Returns (is_ok, stats_dict)
    """
    db = get_performer_db()
    stats = db.get_stats()
    is_ok = stats.get('total_performers', 0) > 0
    return is_ok, stats


def format_performer_info(performer: Performer) -> str:
    """Format performer info for display"""
    lines = [f"🎭 *{performer.name}*"]
    
    if performer.aliases:
        lines.append(f"AKA: {', '.join(performer.aliases[:3])}")
    
    details = []
    if performer.ethnicity:
        details.append(performer.ethnicity)
    if performer.hair_color:
        details.append(f"{performer.hair_color} hair")
    if performer.height:
        details.append(f"{performer.height}cm")
    if performer.measurements:
        details.append(performer.measurements)
    
    if details:
        lines.append(" | ".join(details))
    
    return "\n".join(lines)


if __name__ == '__main__':
    # Test the performer DB
    print("=" * 60)
    print("Performer Database Test")
    print("=" * 60)
    
    db = PerformerDB()
    
    # Initialize if needed
    if not Path(db.db_path).exists():
        print("\nInitializing database...")
        db.init_db()
    
    stats = db.get_stats()
    print(f"\nDatabase Stats:")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    
    # Add a test performer
    test_performer = Performer(
        id="test-123",
        name="Test Performer",
        aliases=["Testy", "TP"],
        gender="FEMALE",
        hair_color="Brunette"
    )
    
    print(f"\nAdding test performer...")
    db.add_performer(test_performer)
    
    print(f"\nSearching for 'test'...")
    results = db.search_performer("test", limit=5)
    for p, score in results:
        print(f"  Found: {p.name} (score: {score:.2%})")
    
    # Try StashDB lookup
    print(f"\nTesting StashDB lookup...")
    performer = db.get_performer_by_id("test-123")
    if performer:
        print(f"  Found by ID: {performer.name}")
    
    stats = db.get_stats()
    print(f"\nFinal Stats:")
    print(f"  Total performers: {stats['total_performers']}")
    
    db._close()
    print("\n✓ Test complete!")
