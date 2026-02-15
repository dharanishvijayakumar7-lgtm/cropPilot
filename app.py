"""
CropPilot - Farmer Decision Support System
A Flask web application with login, weather risk map, farm logbook, and disaster scheme navigator.
Supports multilingual interface for Indian farmers.
"""

import os
import json
import sqlite3
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from functools import wraps

# Load environment variables
load_dotenv()

# Google Gemini AI Configuration
try:
    import google.generativeai as genai
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-2.0-flash')
        GEMINI_ENABLED = True
        print("✅ Gemini AI enabled for voice bot")
    else:
        GEMINI_ENABLED = False
        print("⚠️ GEMINI_API_KEY not set - voice bot will use basic responses")
except ImportError:
    GEMINI_ENABLED = False
    gemini_model = None
    print("⚠️ google-generativeai not installed - voice bot will use basic responses")

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'your-secret-key-change-in-production')

# ==================== MULTILINGUAL SUPPORT ====================

# Load translations from JSON file
def load_translations():
    """Load translations from translations.json file."""
    try:
        with open('translations.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print("⚠️ translations.json not found, using default English")
        return {"languages": {"en": "English"}, "translations": {}}

# Global translations data
TRANSLATIONS_DATA = load_translations()

def get_current_language():
    """Get the current language from session, default to English."""
    return session.get('language', 'en')

def t(key, **kwargs):
    """
    Translation function - returns translated text for the given key.
    Falls back to English if translation not found.
    Supports keyword arguments for string formatting.
    """
    lang = get_current_language()
    translations = TRANSLATIONS_DATA.get('translations', {})
    
    if key in translations:
        # Get translation for current language, fallback to English
        text = translations[key].get(lang, translations[key].get('en', key))
    else:
        text = key
    
    # Apply string formatting if kwargs provided
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, ValueError):
            pass  # If formatting fails, return the original text
    
    return text

def get_available_languages():
    """Get list of available languages."""
    return TRANSLATIONS_DATA.get('languages', {'en': 'English'})

# Make translation function available in all templates
@app.context_processor
def inject_translation_helpers():
    """Inject translation function and language data into all templates."""
    return {
        't': t,
        'current_language': get_current_language(),
        'available_languages': get_available_languages()
    }

# Route to change language
@app.route('/set-language/<lang_code>')
def set_language(lang_code):
    """Set the user's preferred language."""
    available = get_available_languages()
    if lang_code in available:
        session['language'] = lang_code
        flash(f'Language changed to {available[lang_code]}', 'success')
    else:
        flash('Language not supported', 'warning')
    
    # Redirect back to the previous page or dashboard
    return redirect(request.referrer or url_for('dashboard'))

# ==================== HEALTH CHECK ENDPOINT ====================

@app.route('/ping')
def ping():
    """
    Public health check endpoint for cron-job.org and Render.
    Returns HTTP 200 with no auth, no redirects, no DB calls.
    """
    return 'pong', 200

# OpenWeather API configuration
# Get your free API key from: https://openweathermap.org/api
OPENWEATHER_API_KEY = os.getenv('OPENWEATHER_API_KEY', 'YOUR_API_KEY_HERE')
OPENWEATHER_FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"

# Check if API key is configured
if not OPENWEATHER_API_KEY or OPENWEATHER_API_KEY == 'YOUR_API_KEY_HERE':
    print("=" * 60)
    print("⚠️  WARNING: OpenWeather API key not configured!")
    print("   Get a free API key from: https://openweathermap.org/api")
    print("   Then add it to your .env file as:")
    print("   OPENWEATHER_API_KEY=your_actual_api_key")
    print("=" * 60)

# ==================== DATABASE SETUP ====================

def get_db_connection():
    """Get SQLite database connection with error handling."""
    try:
        conn = sqlite3.connect('croppilot.db')
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        print(f"Database connection error: {e}")
        raise

def init_db():
    """Initialize the database with required tables."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Users table for authentication
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                state TEXT,
                district TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Farm logs table for Farmer Management System (linked to user)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS farm_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                crop_name TEXT NOT NULL,
                sowing_date DATE NOT NULL,
                expected_harvest_date DATE,
                money_spent REAL DEFAULT 0,
                money_earned REAL DEFAULT 0,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        """)
        
        # Migration: Add user_id column if it doesn't exist (for old databases)
        try:
            cursor.execute("ALTER TABLE farm_logs ADD COLUMN user_id INTEGER")
            print("✅ Migration: Added user_id column to farm_logs table")
        except sqlite3.OperationalError:
            pass  # Column already exists
        
        conn.commit()
        conn.close()
        print("✅ Database initialized successfully")
    except Exception as e:
        print(f"❌ Database initialization error: {e}")

# Initialize database on startup
try:
    init_db()
except Exception as e:
    print(f"❌ Failed to initialize database: {e}")

# ==================== LOGIN REQUIRED DECORATOR ====================

def login_required(f):
    """Decorator to require login for protected routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please login to access this page.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# ==================== HELPER FUNCTIONS ====================

def load_schemes_data():
    """Load schemes data from JSON file."""
    try:
        with open('schemes.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {"schemes": [], "disaster_types": [], "crops": [], "states": []}

# ==================== INTELLIGENT CROP & DISASTER MAPPING ====================

# Crop category mapping - maps aliases and categories to standard crop names
CROP_CATEGORIES = {
    # Cereals / Grains
    'cereals': ['Rice', 'Wheat', 'Maize', 'Barley', 'Millets', 'Ragi', 'Jowar', 'Bajra', 'Sorghum'],
    'grains': ['Rice', 'Wheat', 'Maize', 'Barley', 'Millets', 'Ragi', 'Jowar', 'Bajra', 'Sorghum'],
    
    # Pulses
    'pulses': ['Chickpea', 'Lentils', 'Pigeon Pea', 'Peas', 'Moong', 'Urad', 'Masoor'],
    'dals': ['Chickpea', 'Lentils', 'Pigeon Pea', 'Peas', 'Moong', 'Urad', 'Masoor'],
    
    # Oilseeds
    'oilseeds': ['Groundnut', 'Mustard', 'Sunflower', 'Soybean', 'Sesame', 'Castor'],
    
    # Vegetables
    'vegetables': ['Potato', 'Onion', 'Tomato', 'Brinjal', 'Cabbage', 'Cauliflower', 'Chilli', 'Garlic', 'Ginger'],
    'sabzi': ['Potato', 'Onion', 'Tomato', 'Brinjal', 'Cabbage', 'Cauliflower', 'Chilli', 'Garlic', 'Ginger'],
    
    # Fruits
    'fruits': ['Banana', 'Mango', 'Papaya', 'Watermelon', 'Muskmelon', 'Cucumber', 'Grapes', 'Orange', 'Apple'],
    'phal': ['Banana', 'Mango', 'Papaya', 'Watermelon', 'Muskmelon', 'Cucumber', 'Grapes', 'Orange', 'Apple'],
    
    # Cash crops
    'cash_crops': ['Cotton', 'Sugarcane', 'Jute', 'Tobacco'],
    
    # Spices
    'spices': ['Turmeric', 'Ginger', 'Chilli', 'Cardamom', 'Pepper', 'Cumin', 'Coriander'],
    'masale': ['Turmeric', 'Ginger', 'Chilli', 'Cardamom', 'Pepper', 'Cumin', 'Coriander'],
    
    # Plantation crops
    'plantation': ['Tea', 'Coffee', 'Coconut', 'Arecanut', 'Rubber', 'Cashew'],
}

# Crop aliases - maps common names/misspellings to standard names
CROP_ALIASES = {
    # Rice aliases
    'paddy': 'Rice', 'dhan': 'Rice', 'chawal': 'Rice', 'rice paddy': 'Rice',
    'dhaan': 'Rice', 'chaawal': 'Rice',
    
    # Wheat aliases
    'gehu': 'Wheat', 'gehun': 'Wheat', 'gahu': 'Wheat', 'atta': 'Wheat',
    
    # Maize aliases
    'corn': 'Maize', 'makka': 'Maize', 'makki': 'Maize', 'bhutta': 'Maize',
    
    # Millets aliases
    'bajra': 'Millets', 'jowar': 'Millets', 'ragi': 'Millets', 'nachni': 'Ragi',
    'sorghum': 'Jowar', 'pearl millet': 'Bajra', 'finger millet': 'Ragi',
    
    # Pulses aliases
    'chana': 'Chickpea', 'gram': 'Chickpea', 'chole': 'Chickpea', 'kabuli chana': 'Chickpea',
    'masoor': 'Lentils', 'dal': 'Lentils', 'arhar': 'Pigeon Pea', 'tur': 'Pigeon Pea', 'toor': 'Pigeon Pea',
    'matar': 'Peas', 'green peas': 'Peas',
    'moong': 'Moong', 'mung': 'Moong', 'green gram': 'Moong',
    'urad': 'Urad', 'black gram': 'Urad',
    
    # Oilseeds aliases
    'sarson': 'Mustard', 'sarso': 'Mustard', 'rai': 'Mustard',
    'moongfali': 'Groundnut', 'mungfali': 'Groundnut', 'peanut': 'Groundnut', 'peanuts': 'Groundnut',
    'til': 'Sesame', 'gingelly': 'Sesame',
    
    # Vegetables aliases
    'aloo': 'Potato', 'alu': 'Potato', 'aaloo': 'Potato',
    'pyaz': 'Onion', 'pyaaz': 'Onion', 'kanda': 'Onion',
    'tamatar': 'Tomato', 'tamaatar': 'Tomato',
    'baingan': 'Brinjal', 'baigan': 'Brinjal', 'eggplant': 'Brinjal', 'aubergine': 'Brinjal',
    'patta gobhi': 'Cabbage', 'band gobhi': 'Cabbage',
    'phool gobhi': 'Cauliflower', 'gobi': 'Cauliflower',
    'mirchi': 'Chilli', 'mirch': 'Chilli', 'green chilli': 'Chilli',
    'lehsun': 'Garlic', 'lahsun': 'Garlic',
    'adrak': 'Ginger', 'adrakh': 'Ginger',
    'haldi': 'Turmeric',
    
    # Fruits aliases
    'kela': 'Banana', 'aam': 'Mango', 'papita': 'Papaya',
    'tarbuz': 'Watermelon', 'tarbooz': 'Watermelon',
    'kharbooja': 'Muskmelon', 'kharbuza': 'Muskmelon',
    'kakdi': 'Cucumber', 'kheera': 'Cucumber',
    'angur': 'Grapes', 'angoor': 'Grapes',
    'santra': 'Orange', 'narangi': 'Orange',
    'seb': 'Apple',
    
    # Cash crops aliases
    'kapas': 'Cotton', 'rui': 'Cotton', 'narma': 'Cotton',
    'ganna': 'Sugarcane', 'ikh': 'Sugarcane', 'ganne': 'Sugarcane',
    'joot': 'Jute', 'pat': 'Jute',
    
    # Plantation aliases
    'chai': 'Tea', 'coffee': 'Coffee',
    'nariyal': 'Coconut', 'narial': 'Coconut',
    'supari': 'Arecanut',
    'kaju': 'Cashew',
}

# Disaster normalization - maps similar disaster types together
DISASTER_MAPPING = {
    # Flood-related
    'flood': ['flood', 'heavy_rain', 'waterlogging', 'inundation'],
    'heavy_rain': ['flood', 'heavy_rain', 'waterlogging'],
    'waterlogging': ['flood', 'waterlogging'],
    
    # Drought-related
    'drought': ['drought', 'water_scarcity', 'dry_spell'],
    'no_rain': ['drought'],
    'water_scarcity': ['drought'],
    'dry_spell': ['drought'],
    
    # Storm-related
    'cyclone': ['cyclone', 'storm', 'hurricane', 'typhoon'],
    'storm': ['cyclone', 'storm'],
    'hurricane': ['cyclone'],
    'typhoon': ['cyclone'],
    
    # Cold-related
    'hailstorm': ['hailstorm', 'frost', 'cold_wave'],
    'frost': ['hailstorm', 'frost', 'cold_wave'],
    'cold_wave': ['frost', 'cold_wave'],
    
    # Heat-related
    'heatwave': ['heatwave', 'heat_stress'],
    'heat_stress': ['heatwave'],
    
    # Pest-related
    'pest_attack': ['pest_attack', 'insect_attack', 'locust'],
    'insect_attack': ['pest_attack'],
    'locust': ['pest_attack'],
    
    # Disease-related
    'disease': ['disease', 'crop_disease', 'fungal_infection'],
    'crop_disease': ['disease'],
    'fungal_infection': ['disease'],
    
    # Others
    'earthquake': ['earthquake', 'landslide'],
    'landslide': ['landslide', 'earthquake'],
    'unseasonal_rain': ['flood', 'hailstorm'],  # Can cause both
    'fire': ['fire'],
}

# Disaster aliases for user input normalization
DISASTER_ALIASES = {
    # Flood
    'baarish': 'heavy_rain', 'barish': 'heavy_rain', 'heavy rain': 'heavy_rain',
    'bahut baarish': 'heavy_rain', 'paani': 'flood', 'baadh': 'flood', 'badh': 'flood',
    
    # Drought
    'sukha': 'drought', 'no rain': 'drought', 'bina baarish': 'drought',
    'paani ki kami': 'drought', 'water shortage': 'drought',
    
    # Storm
    'toofan': 'cyclone', 'toofaan': 'cyclone', 'aandhi': 'storm', 'andhi': 'storm',
    'cyclonic storm': 'cyclone', 'tufan': 'cyclone',
    
    # Hail
    'ole': 'hailstorm', 'hail': 'hailstorm', 'olavrishti': 'hailstorm',
    'barf': 'frost', 'pala': 'frost', 'frost damage': 'frost',
    
    # Heat
    'loo': 'heatwave', 'garmi': 'heatwave', 'heat wave': 'heatwave', 'bahut garmi': 'heatwave',
    
    # Pests
    'keede': 'pest_attack', 'kida': 'pest_attack', 'keeda': 'pest_attack',
    'tiddi': 'pest_attack', 'locust attack': 'pest_attack', 'insects': 'pest_attack',
    
    # Disease
    'rog': 'disease', 'bimari': 'disease', 'beemari': 'disease',
    'fungus': 'disease', 'infection': 'disease',
    
    # Others
    'bhukamp': 'earthquake', 'bhuchal': 'earthquake',
    'pahad girana': 'landslide', 'landslip': 'landslide',
    'aag': 'fire', 'fire': 'fire',
}


def normalize_crop(crop_input):
    """
    Normalize crop name to standard format.
    Returns tuple: (normalized_crop, match_type, related_crops)
    match_type: 'exact', 'alias', 'category', 'fuzzy', 'unknown'
    """
    if not crop_input:
        return (None, 'unknown', [])
    
    crop_lower = crop_input.strip().lower()
    crop_title = crop_input.strip().title()
    
    # Load valid crops from schemes data
    schemes_data = load_schemes_data()
    valid_crops = [c.lower() for c in schemes_data.get('crops', [])]
    
    # 1. Exact match (case-insensitive)
    if crop_lower in valid_crops:
        return (crop_title, 'exact', [])
    
    # 2. Alias match
    if crop_lower in CROP_ALIASES:
        normalized = CROP_ALIASES[crop_lower]
        return (normalized, 'alias', [])
    
    # 3. Category match - returns all crops in that category
    if crop_lower in CROP_CATEGORIES:
        category_crops = CROP_CATEGORIES[crop_lower]
        return (category_crops[0], 'category', category_crops)
    
    # 4. Fuzzy match - find similar crop names
    for standard_crop in valid_crops:
        # Check if input is substring of crop name or vice versa
        if crop_lower in standard_crop or standard_crop in crop_lower:
            return (standard_crop.title(), 'fuzzy', [])
        
        # Check first 4 characters match
        if len(crop_lower) >= 4 and len(standard_crop) >= 4:
            if crop_lower[:4] == standard_crop[:4]:
                return (standard_crop.title(), 'fuzzy', [])
    
    # 5. Check aliases for partial match
    for alias, standard in CROP_ALIASES.items():
        if alias in crop_lower or crop_lower in alias:
            return (standard, 'fuzzy', [])
    
    # 6. Not found - return original with 'unknown' type
    return (crop_title, 'unknown', [])


def normalize_disaster(disaster_input):
    """
    Normalize disaster type to standard format.
    Returns tuple: (normalized_disaster, match_type, related_disasters)
    """
    if not disaster_input:
        return (None, 'unknown', [])
    
    disaster_lower = disaster_input.strip().lower().replace(' ', '_')
    
    # 1. Exact match in disaster mapping
    if disaster_lower in DISASTER_MAPPING:
        return (disaster_lower, 'exact', DISASTER_MAPPING[disaster_lower])
    
    # 2. Check aliases
    if disaster_lower in DISASTER_ALIASES:
        normalized = DISASTER_ALIASES[disaster_lower]
        related = DISASTER_MAPPING.get(normalized, [normalized])
        return (normalized, 'alias', related)
    
    # 3. Check with underscores removed
    disaster_no_underscore = disaster_lower.replace('_', '')
    for key in DISASTER_MAPPING:
        if key.replace('_', '') == disaster_no_underscore:
            return (key, 'fuzzy', DISASTER_MAPPING[key])
    
    # 4. Partial match
    for key in DISASTER_MAPPING:
        if disaster_lower in key or key in disaster_lower:
            return (key, 'fuzzy', DISASTER_MAPPING[key])
    
    # 5. Check aliases for partial match
    for alias, standard in DISASTER_ALIASES.items():
        if alias.replace(' ', '_') in disaster_lower or disaster_lower in alias.replace(' ', '_'):
            related = DISASTER_MAPPING.get(standard, [standard])
            return (standard, 'fuzzy', related)
    
    # 6. Unknown - return general disaster
    return ('general', 'unknown', ['flood', 'drought', 'cyclone', 'hailstorm', 'pest_attack', 'disease'])


def find_eligible_schemes(crop, disaster_type, land_size, has_insurance, damage_percent=50, has_kcc=False):
    """
    Find eligible government schemes with INTELLIGENT matching.
    Features:
    - Crop normalization (aliases, categories, fuzzy matching)
    - Disaster normalization (similar disasters grouped)
    - Scoring-based relevance ranking
    - NEVER returns empty - always provides fallback suggestions
    """
    schemes_data = load_schemes_data()
    eligible_schemes = []
    fallback_schemes = []
    
    # Normalize inputs
    normalized_crop, crop_match_type, related_crops = normalize_crop(crop)
    normalized_disaster, disaster_match_type, related_disasters = normalize_disaster(disaster_type)
    
    # Debug logging (helps future debugging)
    print(f"[Disaster Help] Input crop: '{crop}' → Normalized: '{normalized_crop}' ({crop_match_type})")
    print(f"[Disaster Help] Input disaster: '{disaster_type}' → Normalized: '{normalized_disaster}' ({disaster_match_type})")
    if related_crops:
        print(f"[Disaster Help] Related crops: {related_crops}")
    if related_disasters:
        print(f"[Disaster Help] Related disasters: {related_disasters}")
    
    # Build list of crops to check (includes related crops from category)
    crops_to_check = [normalized_crop]
    if related_crops:
        crops_to_check.extend(related_crops)
    crops_to_check = list(set([c for c in crops_to_check if c]))  # Remove duplicates and None
    
    # Build list of disasters to check
    disasters_to_check = related_disasters if related_disasters else [normalized_disaster]
    disasters_to_check = list(set([d for d in disasters_to_check if d]))
    
    for scheme in schemes_data.get('schemes', []):
        scheme_disasters = scheme.get('disaster_types', [])
        scheme_crops = scheme.get('eligible_crops', [])
        scheme_crops_lower = [c.lower() for c in scheme_crops]
        
        # Calculate match scores
        crop_score = 0
        disaster_score = 0
        match_reasons = []
        
        # Check crop match
        crop_matched = False
        matched_crop_name = None
        
        # Check for "All Crops" or "all" in scheme
        if 'All Crops' in scheme_crops or 'all' in scheme_crops_lower or 'general' in scheme_crops_lower:
            crop_matched = True
            crop_score = 20  # Lower score for generic match
            matched_crop_name = "All Crops"
            match_reasons.append("✓ This scheme covers all crop types")
        else:
            # Check each crop we're looking for
            for check_crop in crops_to_check:
                if check_crop and check_crop.lower() in scheme_crops_lower:
                    crop_matched = True
                    matched_crop_name = check_crop
                    if check_crop.lower() == normalized_crop.lower() if normalized_crop else False:
                        crop_score = 50  # Exact match
                        match_reasons.append(f"✓ Your crop ({crop}) is directly covered")
                    else:
                        crop_score = 35  # Related crop match
                        match_reasons.append(f"✓ Related crop ({check_crop}) is covered")
                    break
        
        # Check disaster match
        disaster_matched = False
        matched_disaster_name = None
        
        # Check for "general" or "all" disaster support
        if 'general' in scheme_disasters or 'all' in scheme_disasters:
            disaster_matched = True
            disaster_score = 20
            matched_disaster_name = "General Disaster"
            match_reasons.append("✓ This scheme covers general disaster relief")
        else:
            for check_disaster in disasters_to_check:
                if check_disaster in scheme_disasters:
                    disaster_matched = True
                    matched_disaster_name = check_disaster
                    if check_disaster == normalized_disaster:
                        disaster_score = 50  # Exact match
                        match_reasons.append(f"✓ Disaster type ({disaster_type.replace('_', ' ').title()}) is covered")
                    else:
                        disaster_score = 35  # Related disaster match
                        match_reasons.append(f"✓ Related disaster type ({check_disaster.replace('_', ' ').title()}) is covered")
                    break
        
        # Check land size
        min_land = scheme.get('min_land_size', 0)
        max_land = scheme.get('max_land_size', 999)
        land_eligible = min_land <= land_size <= max_land
        
        # Check insurance requirement
        insurance_ok = True
        if scheme.get('requires_insurance', False) and not has_insurance:
            insurance_ok = False
        
        # Calculate total priority score
        priority_score = crop_score + disaster_score
        
        # Bonus scores
        if land_eligible:
            match_reasons.append(f"✓ Your land size ({land_size} hectares) meets criteria")
            priority_score += 10
        
        if land_size <= 2:
            match_reasons.append("✓ Small/marginal farmer - priority benefits apply")
            priority_score += 15
        
        if has_insurance and scheme.get('requires_insurance'):
            match_reasons.append("✓ Crop insurance qualifies you for claims")
            priority_score += 20
        elif has_insurance and not scheme.get('requires_insurance'):
            match_reasons.append("✓ Additional insurance coverage available")
            priority_score += 10
        
        if damage_percent >= 75:
            priority_score += 25
            match_reasons.append("✓ Severe damage (>75%) qualifies for maximum relief")
        elif damage_percent >= 50:
            priority_score += 15
            match_reasons.append("✓ Significant damage (50-75%) eligible for relief")
        elif damage_percent >= 33:
            priority_score += 10
            match_reasons.append("✓ Moderate damage (33-50%) may qualify for partial relief")
        
        if has_kcc and 'kcc' in scheme.get('id', '').lower():
            priority_score += 30
            match_reasons.append("✓ KCC holder - eligible for loan restructuring")
        
        # Calculate estimated compensation
        max_amount = scheme.get('max_amount', 0)
        comp_percent = scheme.get('compensation_percent', 100)
        estimated_amount = int((max_amount * min(damage_percent, comp_percent)) / 100)
        
        # Determine match confidence
        if crop_score >= 50 and disaster_score >= 50:
            match_confidence = 'high'
        elif crop_score >= 35 or disaster_score >= 35:
            match_confidence = 'medium'
        else:
            match_confidence = 'low'
        
        scheme_result = {
            'id': scheme.get('id'),
            'name': scheme.get('name'),
            'description': scheme.get('description'),
            'max_amount': max_amount,
            'estimated_amount': estimated_amount,
            'compensation_percent': comp_percent,
            'documents': scheme.get('documents_required', []),
            'steps': scheme.get('application_steps', []),
            'helpline': scheme.get('helpline', 'N/A'),
            'website': scheme.get('website', '#'),
            'reasons': match_reasons,
            'priority_score': priority_score,
            'match_confidence': match_confidence,
            'crop_matched': matched_crop_name,
            'disaster_matched': matched_disaster_name,
        }
        
        # Categorize scheme
        if crop_matched and disaster_matched and land_eligible and insurance_ok:
            # Full match
            eligible_schemes.append(scheme_result)
        elif (crop_matched or disaster_matched) and land_eligible:
            # Partial match - add to fallback
            scheme_result['reasons'].insert(0, "⚡ Partial match - verify eligibility with helpline")
            scheme_result['match_confidence'] = 'low'
            fallback_schemes.append(scheme_result)
        elif scheme.get('id') in ['sdrf', 'pmfby', 'small_farmer_relief']:
            # Always include major government schemes as fallback
            scheme_result['reasons'] = [
                "ℹ️ General relief scheme - may apply based on state declaration",
                f"✓ Land size ({land_size} hectares) eligible" if land_eligible else f"⚠️ Check land size criteria",
            ]
            scheme_result['match_confidence'] = 'low'
            scheme_result['priority_score'] = 5
            fallback_schemes.append(scheme_result)
    
    # Sort by priority score
    eligible_schemes.sort(key=lambda x: x.get('priority_score', 0), reverse=True)
    fallback_schemes.sort(key=lambda x: x.get('priority_score', 0), reverse=True)
    
    # Log results
    print(f"[Disaster Help] Found {len(eligible_schemes)} direct matches, {len(fallback_schemes)} fallback options")
    
    # If we have eligible schemes, return them
    if eligible_schemes:
        # Add top fallbacks as "You may also be eligible" section
        if fallback_schemes:
            # Mark top 2 fallbacks as suggestions and append
            for fb in fallback_schemes[:2]:
                fb['is_suggestion'] = True
                fb['reasons'].insert(0, "💡 You may also qualify for this scheme")
                eligible_schemes.append(fb)
        return eligible_schemes
    
    # No direct matches - return fallback schemes with helpful message
    if fallback_schemes:
        for fb in fallback_schemes:
            fb['reasons'].insert(0, "📋 Showing relevant schemes based on partial match")
        return fallback_schemes
    
    # Absolute fallback - return generic government schemes
    generic_schemes = []
    for scheme in schemes_data.get('schemes', []):
        if scheme.get('id') in ['sdrf', 'pmfby', 'input_subsidy', 'small_farmer_relief']:
            generic_schemes.append({
                'id': scheme.get('id'),
                'name': scheme.get('name'),
                'description': scheme.get('description'),
                'max_amount': scheme.get('max_amount', 0),
                'estimated_amount': int(scheme.get('max_amount', 0) * damage_percent / 100),
                'compensation_percent': scheme.get('compensation_percent', 100),
                'documents': scheme.get('documents_required', []),
                'steps': scheme.get('application_steps', []),
                'helpline': scheme.get('helpline', 'N/A'),
                'website': scheme.get('website', '#'),
                'reasons': [
                    f"📋 General government relief scheme",
                    f"ℹ️ Your crop ({crop}) may qualify - contact helpline to verify",
                    f"ℹ️ Disaster ({disaster_type.replace('_', ' ').title()}) relief available if declared by state",
                    "📞 Call helpline for eligibility confirmation"
                ],
                'priority_score': 5,
                'match_confidence': 'low',
                'is_fallback': True,
            })
    
    if generic_schemes:
        print("[Disaster Help] Returning generic fallback schemes")
        return generic_schemes
    
    # This should never happen, but just in case
    print("[Disaster Help] WARNING: No schemes found at all - returning empty suggestions")
    return [{
        'id': 'contact_helpline',
        'name': 'Contact Kisan Call Center',
        'description': 'No matching schemes found in database. Contact the Kisan Call Center for personalized assistance.',
        'max_amount': 0,
        'estimated_amount': 0,
        'compensation_percent': 0,
        'documents': ['Aadhaar Card', 'Land Records', 'Damage Photos'],
        'steps': [
            'Call 1800-180-1551 (Toll Free)',
            'Explain your crop and disaster situation',
            'Ask about available schemes in your state',
            'Visit nearest Krishi Vigyan Kendra for guidance'
        ],
        'helpline': '1800-180-1551',
        'website': 'https://agricoop.nic.in',
        'reasons': [
            f"⚠️ No exact match found for {crop} + {disaster_type.replace('_', ' ').title()}",
            "📞 Contact helpline for state-specific schemes",
            "🏛️ Visit local agriculture office for assistance"
        ],
        'priority_score': 1,
        'match_confidence': 'low',
        'is_fallback': True,
    }]

# ==================== WEATHER RISK ANALYSIS ====================

def get_weather_forecast_data(lat, lon):
    """Fetch weather forecast from OpenWeather API."""
    if not OPENWEATHER_API_KEY or OPENWEATHER_API_KEY == 'YOUR_API_KEY_HERE' or len(OPENWEATHER_API_KEY) < 20:
        return {
            'success': False,
            'error': 'API key not configured. Please add OPENWEATHER_API_KEY to .env file.'
        }
    
    params = {
        'lat': lat,
        'lon': lon,
        'appid': OPENWEATHER_API_KEY,
        'units': 'metric'
    }
    
    try:
        response = requests.get(OPENWEATHER_FORECAST_URL, params=params, timeout=15)
        
        if response.status_code == 401:
            return {'success': False, 'error': 'Invalid API key. Please check your OpenWeather API key.'}
        
        if response.status_code == 429:
            return {'success': False, 'error': 'API rate limit exceeded. Please try again later.'}
        
        response.raise_for_status()
        data = response.json()
        
        # Process forecast data
        total_rainfall = 0
        humidity_values = []
        temp_values = []
        weather_conditions = []
        clouds_values = []
        
        for item in data.get('list', []):
            temp_values.append(item['main']['temp'])
            humidity_values.append(item['main'].get('humidity', 50))
            clouds_values.append(item.get('clouds', {}).get('all', 0))
            
            # Get weather condition
            if item.get('weather'):
                weather_conditions.append(item['weather'][0].get('main', '').lower())
            
            # Add rainfall if present
            if 'rain' in item:
                total_rainfall += item['rain'].get('3h', 0)
            # Some APIs use '1h' instead
            if 'rain' in item and '1h' in item['rain']:
                total_rainfall += item['rain'].get('1h', 0)
        
        avg_humidity = sum(humidity_values) / len(humidity_values) if humidity_values else 50
        avg_temp = sum(temp_values) / len(temp_values) if temp_values else 25
        avg_clouds = sum(clouds_values) / len(clouds_values) if clouds_values else 0
        city_name = data.get('city', {}).get('name', 'Unknown Location')
        
        # Count rainy/cloudy conditions
        rainy_count = sum(1 for w in weather_conditions if w in ['rain', 'drizzle', 'thunderstorm', 'shower'])
        cloudy_count = sum(1 for w in weather_conditions if w in ['clouds', 'mist', 'fog', 'haze'])
        clear_count = sum(1 for w in weather_conditions if w in ['clear', 'sun'])
        
        # Extract daily forecast for 5 days (API gives 3-hour intervals, so ~8 per day)
        daily_forecasts = []
        forecast_list = data.get('list', [])
        
        # Group forecasts by day and get one representative entry per day
        from collections import defaultdict
        daily_data = defaultdict(list)
        
        for item in forecast_list:
            # Get the date part only
            dt_txt = item.get('dt_txt', '')
            if dt_txt:
                date_str = dt_txt.split(' ')[0]
                daily_data[date_str].append(item)
        
        # Get first 5 days of forecasts
        sorted_dates = sorted(daily_data.keys())[:5]
        for date_str in sorted_dates:
            day_items = daily_data[date_str]
            # Use midday forecast if available, otherwise first
            midday_item = None
            for item in day_items:
                if '12:00:00' in item.get('dt_txt', '') or '15:00:00' in item.get('dt_txt', ''):
                    midday_item = item
                    break
            if not midday_item:
                midday_item = day_items[0]
            
            # Calculate day's rain
            day_rain = sum(it.get('rain', {}).get('3h', 0) for it in day_items)
            
            # Get weather condition
            weather_main = 'clear'
            if midday_item.get('weather'):
                weather_main = midday_item['weather'][0].get('main', 'Clear').lower()
            
            daily_forecasts.append({
                'date': date_str,
                'temp': round(midday_item['main']['temp']),
                'weather': weather_main,
                'rain': round(day_rain, 1),
                'humidity': midday_item['main'].get('humidity', 50)
            })
        
        return {
            'success': True,
            'total_rainfall': round(total_rainfall, 2),
            'avg_humidity': round(avg_humidity, 1),
            'avg_temp': round(avg_temp, 1),
            'avg_clouds': round(avg_clouds, 1),
            'city': city_name,
            'forecast_days': 5,
            'rainy_periods': rainy_count,
            'cloudy_periods': cloudy_count,
            'clear_periods': clear_count,
            'total_periods': len(weather_conditions),
            'daily_forecasts': daily_forecasts
        }
        
    except requests.exceptions.Timeout:
        return {'success': False, 'error': 'Weather API timeout. Please try again.'}
    except requests.exceptions.ConnectionError:
        return {'success': False, 'error': 'No internet connection.'}
    except Exception as e:
        return {'success': False, 'error': f'Error fetching weather: {str(e)}'}

def analyze_village_risk(lat, lon):
    """Analyze weather data and determine village risk level."""
    weather = get_weather_forecast_data(lat, lon)
    
    if not weather['success']:
        return {'success': False, 'error': weather.get('error', 'Failed to fetch weather data')}
    
    # Get detailed location info using reverse geocoding (Nominatim - free API)
    location_info = get_location_details(lat, lon)
    
    total_rainfall = weather['total_rainfall']
    avg_humidity = weather['avg_humidity']
    avg_temp = weather['avg_temp']
    avg_clouds = weather.get('avg_clouds', 0)
    rainy_periods = weather.get('rainy_periods', 0)
    cloudy_periods = weather.get('cloudy_periods', 0)
    total_periods = weather.get('total_periods', 40)
    
    # Calculate estimated rainfall considering multiple factors
    # If there's actual rainfall data, use it; otherwise estimate from humidity and clouds
    if total_rainfall > 0:
        estimated_10day_rainfall = total_rainfall * 2
    else:
        # Estimate rainfall potential from humidity and cloud cover
        # High humidity + high clouds = likely rain soon
        rain_potential = (avg_humidity / 100) * (avg_clouds / 100) * 100
        estimated_10day_rainfall = rain_potential * 0.8  # Scale to reasonable mm
        
        # If there are rainy periods forecast, boost the estimate
        if rainy_periods > 0:
            estimated_10day_rainfall += rainy_periods * 5
    
    # Determine risk level based on multiple factors
    rainy_ratio = rainy_periods / total_periods if total_periods > 0 else 0
    
    # HIGH RAINFALL / FLOOD RISK
    if total_rainfall > 50 or estimated_10day_rainfall > 80 or rainy_ratio > 0.4:
        risk_level = 'flood_risk'
        risk_color = '#3498db'
        risk_title = '🌊 High Rainfall / Flood Risk'
        risk_message = f"Heavy rainfall expected in this area. Estimated {estimated_10day_rainfall:.1f}mm over next 10 days. Humidity: {avg_humidity:.1f}%. Ensure proper drainage and avoid low-lying fields. Consider flood-resistant crop varieties."
    
    # EXCESS MOISTURE
    elif avg_humidity >= 80 or (cloudy_periods + rainy_periods) > total_periods * 0.5:
        risk_level = 'excess_moisture'
        risk_color = '#9b59b6'
        risk_title = '💧 Excess Moisture Conditions'
        risk_message = f"High moisture levels expected. Humidity: {avg_humidity:.1f}%. Cloud cover: {avg_clouds:.0f}%. Risk of fungal diseases and pest issues. Ensure good air circulation and consider preventive spraying."
    
    # GOOD CONDITIONS
    elif 40 <= avg_humidity <= 75 and 20 <= avg_temp <= 35:
        risk_level = 'good'
        risk_color = '#27ae60'
        risk_title = '✅ Good Growing Conditions'
        risk_message = f"Favorable conditions for most crops. Temperature: {avg_temp:.1f}°C. Humidity: {avg_humidity:.1f}%. Expected rainfall: {estimated_10day_rainfall:.1f}mm. Ideal time for sowing and field operations."
    
    # MODERATE CONDITIONS  
    elif 30 <= avg_humidity < 40 or estimated_10day_rainfall < 30:
        risk_level = 'moderate'
        risk_color = '#f39c12'
        risk_title = '⚠️ Moderate / Dry Conditions'
        risk_message = f"Slightly dry conditions expected. Humidity: {avg_humidity:.1f}%. Expected rainfall: {estimated_10day_rainfall:.1f}mm. Consider irrigation planning and mulching to conserve moisture."
    
    # DROUGHT RISK - only when genuinely dry
    elif avg_humidity < 30 and avg_temp > 35 and estimated_10day_rainfall < 10:
        risk_level = 'drought'
        risk_color = '#e74c3c'
        risk_title = '🏜️ High Drought Risk'
        risk_message = f"Very dry conditions with low humidity ({avg_humidity:.1f}%) and high temperature ({avg_temp:.1f}°C). Expected rainfall: {estimated_10day_rainfall:.1f}mm. Implement water conservation, use drought-resistant crops, and plan irrigation."
    
    # HOT CONDITIONS
    elif avg_temp > 38:
        risk_level = 'heat_stress'
        risk_color = '#e67e22'
        risk_title = '🌡️ Heat Stress Risk'
        risk_message = f"High temperatures expected ({avg_temp:.1f}°C). Risk of heat stress to crops. Consider shade nets, increase irrigation frequency, and avoid mid-day field work."
    
    # DEFAULT - NORMAL
    else:
        risk_level = 'normal'
        risk_color = '#27ae60'
        risk_title = '✅ Normal Conditions'
        risk_message = f"Weather conditions are within normal range. Temperature: {avg_temp:.1f}°C. Humidity: {avg_humidity:.1f}%. Expected rainfall: {estimated_10day_rainfall:.1f}mm. Good for regular farming activities."
    
    return {
        'success': True,
        'lat': lat,
        'lon': lon,
        'city': weather['city'],
        'location': location_info,
        'risk_level': risk_level,
        'risk_color': risk_color,
        'risk_title': risk_title,
        'risk_message': risk_message,
        'weather': {
            'total_rainfall_5day': total_rainfall,
            'estimated_rainfall_10day': round(estimated_10day_rainfall, 1),
            'avg_humidity': avg_humidity,
            'avg_temp': avg_temp,
            'avg_clouds': avg_clouds,
            'rainy_periods': rainy_periods,
            'cloudy_periods': cloudy_periods
        },
        'forecast': weather.get('daily_forecasts', [])
    }

def get_location_details(lat, lon):
    """Get detailed location info using OpenStreetMap Nominatim reverse geocoding (free)."""
    try:
        url = f"https://nominatim.openstreetmap.org/reverse"
        params = {
            'lat': lat,
            'lon': lon,
            'format': 'json',
            'addressdetails': 1,
            'zoom': 14  # Village level detail
        }
        headers = {
            'User-Agent': 'CropPilot/1.0 (Farmer Decision Support System)'
        }
        
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        address = data.get('address', {})
        
        # Extract location details
        village = address.get('village') or address.get('hamlet') or address.get('town') or address.get('city') or address.get('suburb', '')
        district = address.get('county') or address.get('state_district') or address.get('district', '')
        state = address.get('state', '')
        country = address.get('country', '')
        
        # Build display name
        display_parts = []
        if village:
            display_parts.append(village)
        if district:
            display_parts.append(district)
        if state:
            display_parts.append(state)
        
        return {
            'village': village,
            'district': district,
            'state': state,
            'country': country,
            'display_name': ', '.join(display_parts) if display_parts else 'Unknown Location',
            'full_address': data.get('display_name', '')
        }
        
    except Exception as e:
        print(f"Reverse geocoding error: {e}")
        return {
            'village': '',
            'district': '',
            'state': '',
            'country': '',
            'display_name': 'Location details unavailable',
            'full_address': ''
        }

# ==================== AUTHENTICATION ROUTES ====================

@app.route('/')
def home():
    """Home page - redirect to dashboard if logged in, else to login."""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    """User login page."""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        phone = request.form.get('phone', '').strip()
        password = request.form.get('password', '')
        
        if not phone or not password:
            flash('Please enter phone number and password.', 'danger')
            return render_template('login.html')
        
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE phone = ?', (phone,)).fetchone()
        conn.close()
        
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['user_name'] = user['name']
            session['user_phone'] = user['phone']
            session['user_state'] = user['state']
            session['user_district'] = user['district']
            flash(f'Welcome back, {user["name"]}!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid phone number or password.', 'danger')
    
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    """User registration page."""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    
    schemes_data = load_schemes_data()
    states = schemes_data.get('states', [])
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        phone = request.form.get('phone', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        state = request.form.get('state', '').strip()
        district = request.form.get('district', '').strip()
        
        # Validation
        if not all([name, phone, password, state, district]):
            flash('All fields are required.', 'danger')
            return render_template('register.html', states=states)
        
        if len(phone) != 10 or not phone.isdigit():
            flash('Please enter a valid 10-digit phone number.', 'danger')
            return render_template('register.html', states=states)
        
        if len(password) < 6:
            flash('Password must be at least 6 characters.', 'danger')
            return render_template('register.html', states=states)
        
        if password != confirm_password:
            flash('Passwords do not match.', 'danger')
            return render_template('register.html', states=states)
        
        # Check if phone already exists
        conn = get_db_connection()
        existing = conn.execute('SELECT id FROM users WHERE phone = ?', (phone,)).fetchone()
        
        if existing:
            conn.close()
            flash('Phone number already registered. Please login.', 'warning')
            return redirect(url_for('login'))
        
        # Create user
        password_hash = generate_password_hash(password)
        cursor = conn.execute(
            'INSERT INTO users (name, phone, password_hash, state, district) VALUES (?, ?, ?, ?, ?)',
            (name, phone, password_hash, state, district)
        )
        conn.commit()
        user_id = cursor.lastrowid
        conn.close()
        
        # Auto-login
        session['user_id'] = user_id
        session['user_name'] = name
        session['user_phone'] = phone
        session['user_state'] = state
        session['user_district'] = district
        
        flash('Registration successful! Welcome to CropPilot.', 'success')
        return redirect(url_for('dashboard'))
    
    return render_template('register.html', states=states)

@app.route('/logout')
def logout():
    """Logout user."""
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

# ==================== DASHBOARD ====================

@app.route('/dashboard')
@login_required
def dashboard():
    """Main dashboard after login."""
    return render_template('dashboard.html')

# ==================== VILLAGE RISK MAP ====================

@app.route('/risk-map')
@login_required
def risk_map():
    """Village Risk Map page."""
    return render_template('index.html')

@app.route('/api/analyze-risk', methods=['POST'])
@login_required
def analyze_risk_api():
    """API endpoint to analyze village risk."""
    try:
        data = request.get_json()
        lat = float(data.get('lat', 0))
        lon = float(data.get('lon', 0))
        
        if not (-90 <= lat <= 90):
            return jsonify({'success': False, 'error': 'Invalid latitude. Must be between -90 and 90.'})
        
        if not (-180 <= lon <= 180):
            return jsonify({'success': False, 'error': 'Invalid longitude. Must be between -180 and 180.'})
        
        result = analyze_village_risk(lat, lon)
        return jsonify(result)
        
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid coordinates. Please enter valid numbers.'})
    except Exception as e:
        return jsonify({'success': False, 'error': f'Error: {str(e)}'})

# ==================== FARM LOGBOOK ====================

@app.route('/inventory')
@login_required
def inventory():
    """Farmer Management System - Farm Logbook page."""
    user_id = session.get('user_id')
    conn = get_db_connection()
    logs = conn.execute(
        'SELECT * FROM farm_logs WHERE user_id = ? ORDER BY created_at DESC',
        (user_id,)
    ).fetchall()
    conn.close()
    return render_template('inventory.html', logs=logs)

@app.route('/inventory/add', methods=['POST'])
@login_required
def add_log():
    """Add a new farm log entry."""
    try:
        user_id = session.get('user_id')
        crop_name = request.form.get('crop_name', '').strip()
        sowing_date = request.form.get('sowing_date', '')
        duration_days = request.form.get('duration_days', '')
        expected_harvest_date = request.form.get('expected_harvest_date', '')
        money_spent = request.form.get('money_spent', 0)
        money_earned = request.form.get('money_earned', 0)
        notes = request.form.get('notes', '').strip()
        
        if not crop_name or not sowing_date:
            flash('Crop name and sowing date are required.', 'danger')
            return redirect(url_for('inventory'))
        
        # Calculate harvest date from duration
        if duration_days and not expected_harvest_date:
            sowing = datetime.strptime(sowing_date, '%Y-%m-%d')
            harvest = sowing + timedelta(days=int(duration_days))
            expected_harvest_date = harvest.strftime('%Y-%m-%d')
        
        money_spent = float(money_spent) if money_spent else 0
        money_earned = float(money_earned) if money_earned else 0
        
        conn = get_db_connection()
        conn.execute("""
            INSERT INTO farm_logs (user_id, crop_name, sowing_date, expected_harvest_date, money_spent, money_earned, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, crop_name, sowing_date, expected_harvest_date, money_spent, money_earned, notes))
        conn.commit()
        conn.close()
        
        flash('Farm log entry added successfully!', 'success')
        
    except Exception as e:
        flash(f'Error adding log: {str(e)}', 'danger')
    
    return redirect(url_for('inventory'))

@app.route('/inventory/delete/<int:log_id>', methods=['POST'])
@login_required
def delete_log(log_id):
    """Delete a farm log entry."""
    try:
        user_id = session.get('user_id')
        conn = get_db_connection()
        conn.execute('DELETE FROM farm_logs WHERE id = ? AND user_id = ?', (log_id, user_id))
        conn.commit()
        conn.close()
        flash('Log entry deleted successfully.', 'success')
    except Exception as e:
        flash(f'Error deleting log: {str(e)}', 'danger')
    
    return redirect(url_for('inventory'))

# ==================== VOICE BOT ====================

@app.route('/voice-bot')
@login_required
def voice_bot():
    """Voice Bot - Voice-enabled farmer assistant."""
    # Clear previous location when entering voice bot page
    session.pop('last_bot_location', None)
    return render_template('voice_bot.html')

@app.route('/api/voice-bot', methods=['POST'])
@login_required
def voice_bot_api():
    """API endpoint for voice bot processing with session-based location memory."""
    data = request.get_json()
    message = data.get('message', '')
    language = data.get('language', 'en')
    location = data.get('location', None)  # GPS location from browser
    
    # Try to extract city from message first
    extracted_city = _extract_city(message)
    
    if extracted_city:
        # User mentioned a city - use it and remember it
        location = extracted_city
        session['last_bot_location'] = extracted_city
    elif location:
        # GPS location provided - convert to location dict format
        if isinstance(location, dict) and 'lat' in location and 'lon' in location:
            # Try to get city name from weather API response later
            session['last_bot_location'] = location
    elif 'last_bot_location' in session:
        # Use remembered location from previous query
        location = session['last_bot_location']
    
    response = generate_bot_response(message, language, location)
    
    # Include location context in response for debugging (optional in production)
    return jsonify({
        'response': response,
        'location_used': location.get('name') if isinstance(location, dict) and 'name' in location else None
    })


# ==================== SMART VOICE BOT ENGINE ====================

# Indian cities with coordinates for weather lookup
INDIAN_CITIES = {
    'mumbai': (19.0760, 72.8777), 'delhi': (28.6139, 77.2090),
    'bangalore': (12.9716, 77.5946), 'bengaluru': (12.9716, 77.5946),
    'chennai': (13.0827, 80.2707), 'hyderabad': (17.3850, 78.4867),
    'kolkata': (22.5726, 88.3639), 'pune': (18.5204, 73.8567),
    'ahmedabad': (23.0225, 72.5714), 'jaipur': (26.9124, 75.7873),
    'lucknow': (26.8467, 80.9462), 'kanpur': (26.4499, 80.3319),
    'nagpur': (21.1458, 79.0882), 'indore': (22.7196, 75.8577),
    'bhopal': (23.2599, 77.4126), 'patna': (25.5941, 85.1376),
    'vadodara': (22.3072, 73.1812), 'ludhiana': (30.9010, 75.8573),
    'agra': (27.1767, 78.0081), 'varanasi': (25.3176, 82.9739),
    'surat': (21.1702, 72.8311), 'nashik': (19.9975, 73.7898),
    'coimbatore': (11.0168, 76.9558), 'vijayawada': (16.5062, 80.6480),
    'mysore': (12.2958, 76.6394), 'mysuru': (12.2958, 76.6394),
    'kochi': (9.9312, 76.2673), 'madurai': (9.9252, 78.1198),
    'amritsar': (31.6340, 74.8723), 'chandigarh': (30.7333, 76.7794),
    'ranchi': (23.3441, 85.3096), 'guwahati': (26.1445, 91.7362),
    'bhubaneswar': (20.2961, 85.8245), 'raipur': (21.2514, 81.6296),
    'noida': (28.5355, 77.3910), 'gurgaon': (28.4595, 77.0266),
    'gurugram': (28.4595, 77.0266), 'faridabad': (28.4089, 77.3178),
    'ghaziabad': (28.6692, 77.4538), 'thane': (19.2183, 72.9781),
    'visakhapatnam': (17.6868, 83.2185), 'vizag': (17.6868, 83.2185),
}

# City name aliases and common misspellings for fuzzy matching
CITY_ALIASES = {
    'bengalore': 'bangalore', 'banglore': 'bangalore', 'bangaluru': 'bangalore',
    'blr': 'bangalore', 'bengaluru': 'bangalore', 'bangalor': 'bangalore',
    'puna': 'pune', 'poona': 'pune',
    'dilli': 'delhi', 'new delhi': 'delhi', 'newdelhi': 'delhi',
    'bombay': 'mumbai', 'mumbay': 'mumbai',
    'kolkatta': 'kolkata', 'calcutta': 'kolkata',
    'hyderbad': 'hyderabad', 'hyd': 'hyderabad', 'hydrabad': 'hyderabad',
    'lucnow': 'lucknow', 'lakhnau': 'lucknow',
    'varanshi': 'varanasi', 'banaras': 'varanasi', 'kashi': 'varanasi',
    'mysuru': 'mysore', 'mysooru': 'mysore',
    'madras': 'chennai', 'chenai': 'chennai',
    'cochin': 'kochi', 'cochine': 'kochi',
    'vishakapatnam': 'visakhapatnam', 'vizak': 'visakhapatnam',
    'ahemdabad': 'ahmedabad', 'ahmadabad': 'ahmedabad',
    'jodpur': 'jodhpur', 'jaipure': 'jaipur',
}

def _extract_city(query):
    """Extract city name from query with fuzzy matching."""
    import re
    q = query.lower()
    
    # First, try direct match
    for city, coords in INDIAN_CITIES.items():
        # Use word boundary matching to avoid partial matches
        if re.search(r'\b' + re.escape(city) + r'\b', q):
            return {'name': city.title(), 'lat': coords[0], 'lon': coords[1]}
    
    # Try alias matching
    for alias, canonical in CITY_ALIASES.items():
        if re.search(r'\b' + re.escape(alias) + r'\b', q):
            if canonical in INDIAN_CITIES:
                coords = INDIAN_CITIES[canonical]
                return {'name': canonical.title(), 'lat': coords[0], 'lon': coords[1]}
    
    # Fuzzy match - find words that are close to city names
    words = q.split()
    for word in words:
        if len(word) >= 4:  # Only match words with 4+ chars
            for city, coords in INDIAN_CITIES.items():
                # Check if word starts with same 3-4 chars (fuzzy)
                if len(city) >= 4 and word[:4] == city[:4]:
                    return {'name': city.title(), 'lat': coords[0], 'lon': coords[1]}
                # Check Levenshtein-like similarity (simple version)
                if len(word) >= 5 and len(city) >= 5:
                    matching = sum(1 for a, b in zip(word, city) if a == b)
                    if matching >= len(city) - 2:  # Allow 2 char difference
                        return {'name': city.title(), 'lat': coords[0], 'lon': coords[1]}
    
    return None

def _get_season():
    """Get current Indian farming season."""
    month = datetime.now().month
    if month in [10, 11, 12, 1, 2, 3]:
        return "Rabi", "October - March"
    elif month in [6, 7, 8, 9]:
        return "Kharif", "June - October"
    return "Zaid", "March - June"

def _load_crops():
    """Load crop database."""
    try:
        with open('crop_data.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {"crops": [], "seasons": {}}

def _load_schemes():
    """Load schemes database."""
    try:
        with open('schemes.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {"schemes": []}

def _fetch_weather(location):
    """Fetch live weather for a location."""
    if not location:
        return None
    try:
        data = get_weather_forecast_data(location['lat'], location['lon'])
        return data if data.get('success') else None
    except:
        return None

def _build_weather_response(location, weather):
    """Build comprehensive weather response from live data."""
    if not weather:
        return None
    city = weather.get('city', location.get('name', 'your area') if location else 'your area')
    fc = weather.get('daily_forecasts', [])
    today = fc[0] if fc else None
    tomorrow = fc[1] if len(fc) > 1 else None
    
    response = f"🌤️ Weather for {city}:\n\n"
    
    if today:
        wx_emoji = {
            'clear': '☀️', 'clouds': '☁️', 'rain': '🌧️', 'drizzle': '🌦️',
            'thunderstorm': '⛈️', 'snow': '❄️', 'mist': '🌫️', 'fog': '🌫️'
        }
        emoji = wx_emoji.get(today['weather'].lower(), '🌡️')
        response += f"{emoji} Today: {today['weather'].title()}, {today['temp']}°C\n"
        response += f"   💧 Humidity: {today['humidity']}%\n"
        if today['rain'] > 0:
            response += f"   🌧️ Rain: {today['rain']}mm\n"
        else:
            response += f"   ☀️ No rain expected\n"
    
    if tomorrow:
        response += f"\n📅 Tomorrow: {tomorrow['weather'].title()}, {tomorrow['temp']}°C"
        if tomorrow['rain'] > 0:
            response += f", Rain {tomorrow['rain']}mm"
        response += "\n"
    
    # Add 5-day outlook
    total_rain = weather.get('total_rainfall', 0)
    avg_temp = weather.get('avg_temp', 25)
    
    response += f"\n📊 5-Day Outlook:\n"
    response += f"   • Avg Temperature: {avg_temp}°C\n"
    response += f"   • Total Rainfall: {total_rain}mm\n"
    
    # Farming recommendations based on weather
    response += "\n🌱 Farming Advice:\n"
    if total_rain > 50:
        response += "   ⚠️ Heavy rain expected! Postpone spraying, ensure drainage."
    elif total_rain > 20:
        response += "   💧 Good rainfall ahead. Reduce irrigation, prepare for wet conditions."
    elif total_rain > 5:
        response += "   ✅ Light rain expected. Good for most crops."
    else:
        if avg_temp > 35:
            response += "   ⚠️ Hot & dry weather. Increase irrigation, add mulch."
        elif avg_temp > 30:
            response += "   💧 Warm & dry. Consider irrigation every 2-3 days."
        else:
            response += "   ✅ Dry weather. Good for harvesting if crops are ready."
    
    return response

def _extract_crop_from_query(query):
    """Extract crop name from query including Hindi names and common misspellings."""
    crop_aliases = {
        # Wheat
        'wheat': 'Wheat', 'gehu': 'Wheat', 'gehun': 'Wheat', 'gahu': 'Wheat',
        'beat': 'Wheat', 'weat': 'Wheat',  # Common speech-to-text errors
        # Rice
        'rice': 'Rice', 'dhan': 'Rice', 'chawal': 'Rice', 'paddy': 'Rice',
        # Maize
        'maize': 'Maize', 'makka': 'Maize', 'makki': 'Maize', 'corn': 'Maize',
        # Mustard
        'mustard': 'Mustard', 'sarson': 'Mustard', 'sarso': 'Mustard', 'rai': 'Mustard',
        # Chickpea
        'chickpea': 'Chickpea', 'chana': 'Chickpea', 'gram': 'Chickpea',
        # Cotton
        'cotton': 'Cotton', 'kapas': 'Cotton', 'rui': 'Cotton',
        # Sugarcane
        'sugarcane': 'Sugarcane', 'ganna': 'Sugarcane', 'ikh': 'Sugarcane',
        # Potato
        'potato': 'Potato', 'aloo': 'Potato', 'alu': 'Potato',
        # Onion
        'onion': 'Onion', 'pyaz': 'Onion', 'pyaaz': 'Onion', 'kanda': 'Onion',
        # Tomato
        'tomato': 'Tomato', 'tamatar': 'Tomato',
        # Groundnut
        'groundnut': 'Groundnut', 'moongfali': 'Groundnut', 'mungfali': 'Groundnut', 'peanut': 'Groundnut',
        # Barley
        'barley': 'Barley', 'jau': 'Barley', 'jow': 'Barley',
        # Millets
        'ragi': 'Ragi', 'bajra': 'Bajra', 'jowar': 'Jowar', 'sorghum': 'Jowar',
    }
    
    q = query.lower()
    for alias, crop in crop_aliases.items():
        if alias in q:
            return crop
    return None

def _build_harvest_response(location, weather, query=None):
    """Build harvest advice from live weather with crop-specific info."""
    season, months = _get_season()
    crop_db = _load_crops()
    season_crops = [c['name'] for c in crop_db.get('crops', []) if c.get('season') == season]
    crop_list = ", ".join(season_crops) if season_crops else "seasonal crops"
    
    # Try to extract specific crop from query
    specific_crop = _extract_crop_from_query(query) if query else None
    
    if weather:
        city = weather.get('city', location.get('name', '') if location else '')
        fc = weather.get('daily_forecasts', [])
        today = fc[0] if fc else None
        tomorrow = fc[1] if len(fc) > 1 else None
        
        if today:
            rain = today.get('rain', 0)
            wx = today.get('weather', 'clear').lower()
            hum = today.get('humidity', 50)
            temp = today.get('temp', 25)
            wind = today.get('wind_speed', 0)  # if available
            loc = f" in {city}" if city else ""
            crop_info = f" for {specific_crop}" if specific_crop else ""
            
            # Detailed condition analysis
            problems = []
            warnings = []
            
            # Rain check
            if rain > 10:
                problems.append(f"🌧️ Heavy rainfall ({rain}mm)")
            elif rain > 2:
                warnings.append(f"🌦️ Light rain possible ({rain}mm)")
            
            # Weather condition check
            if wx in ['rain', 'thunderstorm']:
                problems.append(f"⛈️ {wx.title()} forecast")
            elif wx in ['drizzle', 'shower']:
                warnings.append(f"🌧️ {wx.title()} expected")
            
            # Humidity check
            if hum > 85:
                problems.append(f"💧 Very high humidity ({hum}%)")
            elif hum > 75:
                warnings.append(f"💧 High humidity ({hum}%) - harvest early morning")
            
            # Temperature check for specific crops
            if specific_crop == 'Wheat' and temp > 35:
                warnings.append(f"🌡️ High temp ({temp}°C) - harvest early to avoid grain shattering")
            
            # Build response
            if problems:
                response = f"❌ NOT recommended for harvesting today{loc}{crop_info}\n\n"
                response += "⚠️ Issues:\n"
                for p in problems:
                    response += f"   • {p}\n"
                if warnings:
                    response += "\n⚡ Also note:\n"
                    for w in warnings:
                        response += f"   • {w}\n"
                
                # Recommend best day from forecast
                best_day = None
                for i, day in enumerate(fc[1:4], 1):  # Check next 3 days
                    if day.get('rain', 0) < 2 and day.get('humidity', 50) < 80:
                        if day.get('weather', '').lower() not in ['rain', 'thunderstorm', 'drizzle']:
                            best_day = day
                            break
                
                if best_day:
                    response += f"\n✅ Better day: {best_day['date']} ({best_day['weather'].title()}, {best_day['temp']}°C, Humidity {best_day.get('humidity', 50)}%)"
                else:
                    response += "\n📅 Check weather again tomorrow for better conditions."
                
            elif warnings:
                response = f"⚠️ Possible to harvest today{loc}{crop_info}, but with caution:\n\n"
                response += f"🌡️ Current: {wx.title()}, {temp}°C, Humidity {hum}%\n\n"
                response += "⚡ Considerations:\n"
                for w in warnings:
                    response += f"   • {w}\n"
                response += "\n💡 Tip: Harvest between 10 AM - 4 PM when moisture is lowest."
                
            else:
                response = f"✅ EXCELLENT conditions for harvesting today{loc}{crop_info}!\n\n"
                response += f"🌤️ Weather: {wx.title()}\n"
                response += f"🌡️ Temperature: {temp}°C\n"
                response += f"💧 Humidity: {hum}%\n"
                response += f"🌧️ Rain: {rain}mm\n\n"
                response += "💡 Best time: 10 AM - 4 PM for dry grain.\n"
                if specific_crop:
                    response += f"\n🌾 {specific_crop} harvesting tips:\n"
                    if specific_crop == 'Wheat':
                        response += "   • Harvest when grain is hard and golden\n   • Moisture should be <14% for storage"
                    elif specific_crop == 'Rice':
                        response += "   • Harvest when 80% grains are golden\n   • Dry immediately after cutting"
            
            response += f"\n\n📆 {season} season crops: {crop_list}"
            return response
    
    return (f"🔍 I need your location to check harvest conditions{' for ' + specific_crop if specific_crop else ''}.\n\n"
            f"Try:\n"
            f"• 'Can I harvest wheat today in Pune'\n"
            f"• 'Kya aaj Delhi mein katai kar sakta hun'\n\n"
            f"Current {season} season ({months}) crops: {crop_list}")

def _build_crop_response(location, weather, query):
    """Build crop recommendation from real data."""
    season, months = _get_season()
    crop_db = _load_crops()
    all_crops = crop_db.get('crops', [])
    season_crops = [c for c in all_crops if c.get('season') == season]
    
    city = ""
    temp = None
    if weather:
        city = weather.get('city', '')
        temp = weather.get('avg_temp')
    elif location:
        city = location.get('name', '')
    
    # Check for specific crop in query
    q = query.lower()
    specific = None
    for crop in all_crops:
        if crop['name'].lower() in q:
            specific = crop
            break
    
    if specific:
        name = specific['name']
        traits = []
        if specific.get('drought_tolerant'):
            traits.append("drought-tolerant")
        if specific.get('flood_tolerant'):
            traits.append("flood-tolerant")
        trait_str = f" ({', '.join(traits)})" if traits else ""
        
        response = f"{name}{trait_str}:\n"
        response += f"• Season: {specific['season']} ({crop_db.get('seasons', {}).get(specific['season'], {}).get('months', '')})\n"
        response += f"• Temperature: {specific['min_temp']}–{specific['max_temp']}°C\n"
        response += f"• Rainfall need: {specific['rainfall_need']}\n"
        response += f"• Growing period: {specific['growing_days']} days\n"
        response += f"• Best soil: {', '.join(specific['soil_types'])}"
        
        if temp:
            if specific['min_temp'] <= temp <= specific['max_temp']:
                response += f"\n✅ Current {city} temp ({temp}°C) is suitable for {name}."
            else:
                response += f"\n⚠️ Current {city} temp ({temp}°C) is outside ideal range."
        
        if specific['season'] != season:
            response += f"\nNote: {name} is a {specific['season']} crop. Current season is {season}."
        return response
    
    # General recommendation
    if temp and season_crops:
        matched = [c['name'] for c in season_crops if c['min_temp'] <= temp <= c['max_temp']]
        if matched:
            loc = f" in {city}" if city else ""
            return (f"Best crops for {season} season{loc} ({temp}°C):\n"
                    f"{', '.join(matched)}\n\n"
                    f"Season: {months}. Ask about a specific crop for detailed info!")
    
    if season_crops:
        names = ", ".join(c['name'] for c in season_crops)
        return (f"Current {season} season ({months}).\n"
                f"Suitable crops: {names}\n\n"
                f"Tell me your city for weather-matched recommendations!")
    return None

def _build_scheme_response(query):
    """Build scheme info from database."""
    schemes = _load_schemes().get('schemes', [])
    q = query.lower()
    
    # Check for specific scheme
    for s in schemes:
        if s['id'].lower() in q or any(w in q for w in s['name'].lower().split() if len(w) > 3):
            return (f"{s['name']}\n\n"
                    f"{s['description']}\n\n"
                    f"💰 Max compensation: ₹{s['max_amount']:,}\n"
                    f"📞 Helpline: {s['helpline']}\n"
                    f"🌐 Website: {s['website']}")
    
    # List all schemes
    lines = ["Government schemes for farmers:\n"]
    for s in schemes[:5]:
        lines.append(f"• {s['name']} — up to ₹{s['max_amount']:,}")
    lines.append("\nAsk about a specific scheme (e.g., 'tell me about PMFBY') for details.")
    lines.append("Or use Dashboard → Disaster Help to check your eligibility.")
    return "\n".join(lines)

def _build_price_response(query):
    """Build market price info."""
    crop_db = _load_crops()
    q = query.lower()
    
    for crop in crop_db.get('crops', []):
        if crop['name'].lower() in q:
            return (f"Market prices for {crop['name']}:\n\n"
                    f"• eNAM Portal: enam.gov.in (live prices from 1000+ mandis)\n"
                    f"• Agmarknet: agmarknet.gov.in (APMC mandi rates)\n"
                    f"• Kisan Call Center: 1800-180-1551 (toll-free)\n\n"
                    f"{crop['name']} is a {crop['season']} crop. "
                    f"Government MSP rates are revised each season.")
    
    return ("For live market prices:\n\n"
            "• eNAM Portal: enam.gov.in — prices across 1000+ mandis\n"
            "• Agmarknet: agmarknet.gov.in — daily APMC rates\n"
            "• Kisan Call Center: 1800-180-1551 (free)\n\n"
            "Ask for a specific crop (e.g., 'wheat price', 'rice rate') for targeted info!")

def _build_pest_response(query):
    """Build pest management info."""
    crop_db = _load_crops()
    q = query.lower()
    
    for crop in crop_db.get('crops', []):
        if crop['name'].lower() in q:
            return (f"Pest & Disease Management for {crop['name']}:\n\n"
                    f"1. Scout fields weekly for yellowing, spots, or holes\n"
                    f"2. Use IPM (Integrated Pest Management):\n"
                    f"   - Neem oil spray for mild infestations\n"
                    f"   - Targeted chemicals for severe cases\n"
                    f"3. Ensure proper spacing and drainage\n\n"
                    f"📞 Kisan Helpline: 1800-180-1551 for expert diagnosis")
    
    return ("Common Crop Problems:\n\n"
            "• Yellow leaves → Nutrient deficiency (try urea/NPK fertilizer)\n"
            "• Brown spots → Fungal infection (apply fungicide)\n"
            "• Holes in leaves → Insect attack (neem oil or pesticide)\n\n"
            "📞 Kisan Helpline: 1800-180-1551\n"
            "Visit your nearest Krishi Vigyan Kendra for lab diagnosis.")


# ==================== FOCUSED VOICE BOT ENGINE ====================
# This voice bot is LIMITED to 5 high-impact farming questions:
# 1. Weather today / rain prediction
# 2. Harvesting suitability (yes/no)
# 3. Pesticide spraying (yes/no)
# 4. Heatwave / flood / heavy rain alerts
# 5. Pest risk warning
# ==================================================================

# Intent keywords for strict classification
VOICE_BOT_INTENTS = {
    'WEATHER': [
        'weather', 'rain', 'temperature', 'temp', 'forecast',
        'mausam', 'barish', 'baarish', 'barsaat', 'varsha',
        'aaj ka mausam', 'kal ka mausam', 'today weather',
        'will it rain', 'kya barish hogi', 'rain today',
        'hot', 'cold', 'garam', 'thand', 'humidity'
    ],
    'HARVEST': [
        'harvest', 'harvesting', 'cut crop', 'ready to harvest',
        'katai', 'kaat', 'katne', 'fasal katna', 'fasal kaatna',
        'can i harvest', 'should i harvest', 'is today good for harvest',
        'harvesting today', 'kya aaj katai', 'aaj harvest', 
        'ugai', 'upaj lena', 'crop cutting'
    ],
    'SPRAY': [
        'spray', 'pesticide', 'chemical', 'spraying', 'dawai',
        'keetnaashak', 'keetnashak', 'dawa', 'chidkav',
        'should i spray', 'can i spray', 'pesticide spray',
        'spray today', 'aaj spray', 'kya spray karun',
        'fungicide', 'insecticide', 'herbicide'
    ],
    'ALERT': [
        'flood', 'heat', 'heatwave', 'storm', 'heavy rain',
        'baadh', 'badh', 'toofan', 'toofaan', 'andhi', 'aandhi',
        'loo', 'garmi', 'heat wave', 'cyclone', 'warning',
        'alert', 'danger', 'risk', 'khatrha', 'khatarnak',
        'disaster', 'emergency', 'severe', 'extreme'
    ],
    'PEST': [
        'pest', 'insect', 'disease', 'bug', 'worm',
        'kida', 'keeda', 'kide', 'keede', 'makode',
        'rog', 'bimari', 'beemari', 'infection',
        'pest risk', 'pest attack', 'crop disease',
        'fungus', 'leaf spot', 'yellowing', 'wilting'
    ]
}

# Fallback response for out-of-scope questions
FALLBACK_RESPONSE = (
    "🚜 I am currently trained to answer questions about:\n\n"
    "• Today's weather & rain prediction\n"
    "• Is it good to harvest today?\n"
    "• Should I spray pesticides today?\n"
    "• Heatwave / flood / heavy rain alerts\n"
    "• Pest risk warnings\n\n"
    "Please ask one of these questions for accurate advice."
)


def _classify_intent(message):
    """
    Classify user message into one of the supported intents.
    Returns the intent name or None if no match.
    """
    msg = message.lower()
    
    # Check each intent's keywords
    for intent, keywords in VOICE_BOT_INTENTS.items():
        for keyword in keywords:
            if keyword in msg:
                return intent
    
    return None


def _build_focused_weather_response(location, weather):
    """Build a short, actionable weather response."""
    if not weather:
        return ("🌤️ Please tell me your city for weather info.\n"
                "Example: 'Weather in Pune' or 'Delhi mein mausam'")
    
    city = weather.get('city', 'your area')
    fc = weather.get('daily_forecasts', [])
    today = fc[0] if fc else None
    
    if not today:
        return f"⚠️ Unable to fetch weather data for {city}. Try again later."
    
    temp = today.get('temp', 25)
    rain = today.get('rain', 0)
    humidity = today.get('humidity', 50)
    wx = today.get('weather', 'clear').lower()
    
    # Build short, clear response
    response = f"🌤️ Weather in {city} today:\n\n"
    response += f"🌡️ Temperature: {temp}°C\n"
    response += f"💧 Humidity: {humidity}%\n"
    
    if rain > 0 or wx in ['rain', 'drizzle', 'thunderstorm']:
        response += f"🌧️ Rain: {rain}mm expected\n\n"
        response += "💡 Recommendation: Expect rain today. Plan indoor activities."
    else:
        response += f"☀️ Rain: No rain expected\n\n"
        response += "💡 Recommendation: Clear day. Good for outdoor farm work."
    
    return response


def _build_focused_harvest_response(location, weather):
    """Build a YES/NO harvest recommendation."""
    if not weather:
        return ("🌾 Please tell me your city to check harvest conditions.\n"
                "Example: 'Can I harvest in Pune today?'")
    
    city = weather.get('city', 'your area')
    fc = weather.get('daily_forecasts', [])
    today = fc[0] if fc else None
    
    if not today:
        return f"⚠️ Unable to fetch weather for {city}. Try again later."
    
    rain = today.get('rain', 0)
    humidity = today.get('humidity', 50)
    wx = today.get('weather', 'clear').lower()
    
    # Decision logic
    is_bad = (rain > 2 or humidity > 80 or wx in ['rain', 'thunderstorm', 'drizzle'])
    
    if is_bad:
        reasons = []
        if rain > 2:
            reasons.append(f"rain expected ({rain}mm)")
        if humidity > 80:
            reasons.append(f"high humidity ({humidity}%)")
        if wx in ['rain', 'thunderstorm', 'drizzle']:
            reasons.append(f"{wx} forecast")
        
        return (f"❌ NO - Not good for harvesting in {city} today.\n\n"
                f"Reason: {', '.join(reasons)}.\n\n"
                f"💡 Recommendation: Wait for a dry day. Wet harvest causes grain damage and storage problems.")
    else:
        return (f"✅ YES - Good conditions for harvesting in {city} today!\n\n"
                f"Weather: {wx.title()}, {today.get('temp', 25)}°C, Humidity {humidity}%\n\n"
                f"💡 Recommendation: Harvest between 10 AM - 4 PM for best results.")


def _build_focused_spray_response(location, weather):
    """Build a YES/NO pesticide spraying recommendation."""
    if not weather:
        return ("🧴 Please tell me your city to check spray conditions.\n"
                "Example: 'Should I spray pesticide in Delhi?'")
    
    city = weather.get('city', 'your area')
    fc = weather.get('daily_forecasts', [])
    today = fc[0] if fc else None
    
    if not today:
        return f"⚠️ Unable to fetch weather for {city}. Try again later."
    
    rain = today.get('rain', 0)
    wind = today.get('wind_speed', 5)  # Default moderate wind
    humidity = today.get('humidity', 50)
    wx = today.get('weather', 'clear').lower()
    temp = today.get('temp', 25)
    
    # Decision logic for spraying
    problems = []
    
    if rain > 1 or wx in ['rain', 'thunderstorm', 'drizzle']:
        problems.append("rain will wash away chemicals")
    if humidity > 85:
        problems.append("high humidity reduces effectiveness")
    if temp > 35:
        problems.append("high temperature causes rapid evaporation")
    if wind > 15:
        problems.append("strong wind causes drift")
    
    if problems:
        return (f"❌ NO - Avoid spraying in {city} today.\n\n"
                f"Reason: {', '.join(problems)}.\n\n"
                f"💡 Recommendation: Spray early morning (6-9 AM) on a calm, dry day for best results.")
    else:
        return (f"✅ YES - Good conditions for spraying in {city} today.\n\n"
                f"Weather: {wx.title()}, {temp}°C, Humidity {humidity}%, Low rain risk\n\n"
                f"💡 Recommendation: Spray early morning (6-9 AM) or late evening (4-6 PM). Wear protective gear.")


def _build_focused_alert_response(location, weather):
    """Build weather alert response for floods, heatwaves, storms."""
    if not weather:
        return ("⚠️ Please tell me your city to check weather alerts.\n"
                "Example: 'Any flood alert in Mumbai?'")
    
    city = weather.get('city', 'your area')
    fc = weather.get('daily_forecasts', [])
    total_rain = weather.get('total_rainfall', 0)
    avg_temp = weather.get('avg_temp', 25)
    
    alerts = []
    
    # Check for heavy rain / flood risk
    if total_rain > 100:
        alerts.append(("🌊 FLOOD RISK", f"Heavy rainfall ({total_rain}mm) expected in 5 days. Risk of waterlogging."))
    elif total_rain > 50:
        alerts.append(("🌧️ HEAVY RAIN", f"Significant rainfall ({total_rain}mm) expected. Ensure proper drainage."))
    
    # Check for heatwave
    if avg_temp > 40:
        alerts.append(("🔥 HEATWAVE ALERT", f"Extreme heat ({avg_temp}°C avg). Irrigate crops, provide shade for livestock."))
    elif avg_temp > 35:
        alerts.append(("☀️ HIGH HEAT", f"Hot weather ({avg_temp}°C avg). Increase irrigation frequency."))
    
    # Check for storms in forecast
    for day in fc[:3]:
        if day.get('weather', '').lower() in ['thunderstorm', 'storm']:
            alerts.append(("⛈️ STORM WARNING", f"Thunderstorm expected on {day.get('date', 'upcoming days')}. Secure farm equipment."))
            break
    
    if alerts:
        response = f"⚠️ Weather Alerts for {city}:\n\n"
        for alert_type, description in alerts:
            response += f"{alert_type}\n{description}\n\n"
        response += "💡 Take precautions and monitor updates regularly."
        return response
    else:
        return (f"✅ No severe weather alerts for {city}.\n\n"
                f"5-day outlook: {total_rain}mm rain, {avg_temp}°C avg temp.\n\n"
                f"💡 Normal conditions expected. Continue regular farm activities.")


def _build_focused_pest_response(location, weather):
    """Build pest risk warning based on weather conditions."""
    if not weather:
        return ("🐛 Please tell me your city to check pest risk.\n"
                "Example: 'Pest risk in Bangalore?'")
    
    city = weather.get('city', 'your area')
    fc = weather.get('daily_forecasts', [])
    today = fc[0] if fc else None
    avg_humidity = weather.get('avg_humidity', 50)
    avg_temp = weather.get('avg_temp', 25)
    total_rain = weather.get('total_rainfall', 0)
    
    risks = []
    
    # High humidity + warm = fungal diseases
    if avg_humidity > 75 and avg_temp > 25:
        risks.append("🍄 Fungal disease risk HIGH (humid + warm conditions)")
    
    # Recent rain + warmth = insect breeding
    if total_rain > 20 and avg_temp > 20:
        risks.append("🐛 Insect pest risk HIGH (post-rain breeding conditions)")
    
    # Hot and dry = aphids, mites
    if avg_temp > 35 and avg_humidity < 40:
        risks.append("🦗 Aphid/mite risk HIGH (hot, dry conditions)")
    
    # Continuous wet = bacterial/viral spread
    if total_rain > 50:
        risks.append("🦠 Bacterial/viral spread risk (waterlogged fields)")
    
    if risks:
        response = f"⚠️ Pest Risk Alert for {city}:\n\n"
        for risk in risks:
            response += f"• {risk}\n"
        response += "\n💡 Recommendation: Scout fields daily. Apply preventive spray if needed. Consult local KVK for specific treatment."
        return response
    else:
        return (f"✅ Low pest risk in {city} this week.\n\n"
                f"Conditions: {avg_temp}°C avg, {avg_humidity}% humidity, {total_rain}mm rain\n\n"
                f"💡 Recommendation: Continue regular monitoring. Maintain field hygiene.")


def generate_bot_response(message, language, location=None):
    """
    FOCUSED Voice Bot Response Generator.
    
    This bot ONLY handles 5 types of questions:
    1. Weather today / rain prediction
    2. Harvesting suitability (yes/no)
    3. Pesticide spraying (yes/no)
    4. Heatwave / flood / heavy rain alerts
    5. Pest risk warning
    
    All other questions return a fallback response.
    """
    msg = message.lower()
    
    # Handle simple greetings
    if any(kw in msg for kw in ['hello', 'hi', 'namaste', 'namaskar']) and len(msg.split()) <= 3:
        return ("🙏 Namaste! I'm CropPilot Voice Assistant.\n\n"
                "I can help you with:\n"
                "• Today's weather\n"
                "• Harvest suitability\n"
                "• Pesticide spraying advice\n"
                "• Weather alerts\n"
                "• Pest risk warnings\n\n"
                "How can I help you today?")
    
    # Handle thanks
    if any(kw in msg for kw in ['thank', 'dhanyavaad', 'shukriya']):
        return "🙏 You're welcome! Happy farming! Feel free to ask again anytime."
    
    # Extract location from query
    extracted = _extract_city(message)
    if extracted:
        location = extracted
    
    # Fetch live weather if we have location
    weather = _fetch_weather(location) if location else None
    
    # === STRICT INTENT CLASSIFICATION ===
    intent = _classify_intent(message)
    
    if intent == 'WEATHER':
        return _build_focused_weather_response(location, weather)
    
    elif intent == 'HARVEST':
        return _build_focused_harvest_response(location, weather)
    
    elif intent == 'SPRAY':
        return _build_focused_spray_response(location, weather)
    
    elif intent == 'ALERT':
        return _build_focused_alert_response(location, weather)
    
    elif intent == 'PEST':
        return _build_focused_pest_response(location, weather)
    
    else:
        # NO MATCH - Return strict fallback
        # Do NOT attempt to guess or use AI for out-of-scope questions
        return FALLBACK_RESPONSE


def get_ai_farming_response(user_query, language):
    """Get AI-powered response for farming queries using Gemini."""
    if not GEMINI_ENABLED or not gemini_model:
        return None
    
    # Language name mapping
    lang_names = {
        'en': 'English',
        'hi': 'Hindi',
        'kn': 'Kannada',
        'ta': 'Tamil',
        'te': 'Telugu',
        'ml': 'Malayalam'
    }
    response_language = lang_names.get(language, 'English')
    
    # Get current season for context
    season, months = _get_season()
    current_date = datetime.now().strftime("%B %d, %Y")
    
    # Create a focused prompt for farming assistance
    system_prompt = f"""You are CropPilot, an expert AI agricultural advisor for Indian farmers.

CONTEXT:
- Current Date: {current_date}
- Current Season: {season} ({months})
- Common Rabi crops: Wheat, Barley, Chickpea, Mustard, Lentils
- Common Kharif crops: Rice, Maize, Cotton, Sugarcane, Groundnut
- Zaid crops: Watermelon, Cucumber, Muskmelon

RULES:
1. Answer ONLY farming/agriculture questions. For non-farming questions, politely redirect.
2. Give specific, actionable advice - not generic statements.
3. Respond in {response_language}. Use simple language farmers understand.
4. Keep responses concise (3-5 sentences). Use bullet points for multiple tips.
5. Include relevant numbers: ideal temperatures, water needs, timing, etc.
6. Mention government helplines when relevant: Kisan Call Center 1800-180-1551
7. For harvest questions: Consider humidity (<80%), no rain, clear weather.
8. For pest issues: Recommend IPM first, chemicals as last resort.

USER QUERY: {user_query}

Provide a helpful, accurate response with specific recommendations:"""

    try:
        response = gemini_model.generate_content(
            system_prompt,
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=400,
                temperature=0.6
            )
        )
        
        if response and response.text:
            return response.text.strip()
        return None
    except Exception as e:
        print(f"Gemini API error: {e}")
        return None

# ==================== DISASTER SCHEME NAVIGATOR ====================

@app.route('/disaster-help')
@login_required
def disaster_form():
    """Disaster Scheme Navigator - Input form."""
    from datetime import date
    schemes_data = load_schemes_data()
    crops = schemes_data.get('crops', [])
    disaster_types = schemes_data.get('disaster_types', [])
    states = schemes_data.get('states', [])
    today = date.today().isoformat()
    return render_template('disaster_form.html', 
                          crops=crops, 
                          disaster_types=disaster_types,
                          states=states,
                          today=today)

@app.route('/disaster-result', methods=['POST'])
@login_required
def disaster_result():
    """Disaster Scheme Navigator - Results page with enhanced matching."""
    try:
        from datetime import datetime, date
        
        crop = request.form.get('crop', '')
        disaster_type = request.form.get('disaster_type', '')
        land_size = float(request.form.get('land_size', 0))
        has_insurance = request.form.get('has_insurance', 'no') == 'yes'
        has_kcc = request.form.get('has_kcc', 'no') == 'yes'
        damage_percent = int(request.form.get('damage_percent', 50))
        disaster_date_str = request.form.get('disaster_date', '')
        state = request.form.get('state', '')
        
        if not crop or not disaster_type or land_size <= 0:
            flash('Please fill all required fields.', 'danger')
            return redirect(url_for('disaster_form'))
        
        # Calculate days since disaster
        days_since_disaster = 0
        if disaster_date_str:
            try:
                disaster_date = datetime.strptime(disaster_date_str, '%Y-%m-%d').date()
                days_since_disaster = (date.today() - disaster_date).days
            except ValueError:
                days_since_disaster = 0
        
        # Get disaster info for display
        schemes_data = load_schemes_data()
        disaster_info = next(
            (d for d in schemes_data.get('disaster_types', []) if d['id'] == disaster_type),
            {'id': disaster_type, 'name': disaster_type.replace('_', ' ').title(), 'icon': '⚠️'}
        )
        
        # Find eligible schemes with enhanced matching
        schemes = find_eligible_schemes(crop, disaster_type, land_size, has_insurance, damage_percent, has_kcc)
        
        # Calculate total potential compensation
        total_potential = sum(s.get('estimated_amount', 0) for s in schemes)
        
        return render_template(
            'disaster_result.html',
            crop=crop,
            disaster_info=disaster_info,
            land_size=land_size,
            has_insurance=has_insurance,
            has_kcc=has_kcc,
            damage_percent=damage_percent,
            days_since_disaster=days_since_disaster,
            state=state,
            schemes=schemes,
            total_potential=total_potential
        )
        
    except ValueError:
        flash('Please enter valid values.', 'danger')
        return redirect(url_for('disaster_form'))
    except Exception as e:
        flash(f'An error occurred: {str(e)}', 'danger')
        return redirect(url_for('disaster_form'))

# ==================== MAIN ====================

# ==================== GLOBAL ERROR HANDLERS ====================

@app.errorhandler(404)
def page_not_found(e):
    """Handle 404 errors with friendly message."""
    return render_template('login.html'), 404

@app.errorhandler(500)
def internal_error(e):
    """Handle 500 errors with friendly message."""
    return "<h1>Something went wrong</h1><p>Please try again later or contact support.</p><a href='/'>Go Home</a>", 500

@app.errorhandler(Exception)
def handle_exception(e):
    """Handle unexpected exceptions gracefully."""
    print(f"Unhandled exception: {e}")
    return "<h1>Oops! Something went wrong</h1><p>Please try again later.</p><a href='/'>Go Home</a>", 500

if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("🌾 CropPilot - Starting...")
    print("=" * 60)
    
    if OPENWEATHER_API_KEY and OPENWEATHER_API_KEY != 'YOUR_API_KEY_HERE' and len(OPENWEATHER_API_KEY) > 20:
        print("✅ OpenWeather API key configured")
    else:
        print("⚠️  OpenWeather API key NOT configured")
        print("   Get free API key: https://openweathermap.org/api")
        print("   Add to .env file: OPENWEATHER_API_KEY=your_key")
    
    print("\n🌐 Open http://127.0.0.1:5000 in your browser")
    print("=" * 60 + "\n")
    
    # Use DEBUG from environment, default to False for production safety
    DEBUG_MODE = os.getenv('FLASK_DEBUG', 'False').lower() in ('true', '1', 'yes')
    app.run(debug=DEBUG_MODE, host='0.0.0.0', port=5000)
