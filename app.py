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

def t(key):
    """
    Translation function - returns translated text for the given key.
    Falls back to English if translation not found.
    """
    lang = get_current_language()
    translations = TRANSLATIONS_DATA.get('translations', {})
    
    if key in translations:
        # Get translation for current language, fallback to English
        return translations[key].get(lang, translations[key].get('en', key))
    return key

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
    """Get SQLite database connection."""
    conn = sqlite3.connect('croppilot.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize the database with required tables."""
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

# Initialize database on startup
init_db()

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

def find_eligible_schemes(crop, disaster_type, land_size, has_insurance, damage_percent=50, has_kcc=False):
    """Find eligible government schemes based on farmer inputs with priority scoring."""
    schemes_data = load_schemes_data()
    eligible_schemes = []
    
    for scheme in schemes_data.get('schemes', []):
        # Check disaster type match
        if disaster_type not in scheme.get('disaster_types', []):
            continue
        
        # Check crop eligibility
        eligible_crops = scheme.get('eligible_crops', [])
        if crop not in eligible_crops and 'All Crops' not in eligible_crops:
            continue
        
        # Check land size
        min_land = scheme.get('min_land_size', 0)
        max_land = scheme.get('max_land_size', 999)
        if land_size < min_land or land_size > max_land:
            continue
        
        # Check insurance requirement
        if scheme.get('requires_insurance', False) and not has_insurance:
            continue
        
        # Calculate priority score (higher = better match)
        priority_score = 0
        
        # Build eligibility reasons
        reasons = []
        reasons.append(f"✓ Your crop ({crop}) is covered under this scheme")
        reasons.append(f"✓ Disaster type ({disaster_type.replace('_', ' ').title()}) is eligible")
        reasons.append(f"✓ Your land size ({land_size} hectares) meets the criteria")
        
        if has_insurance and scheme.get('requires_insurance'):
            reasons.append("✓ You have crop insurance which qualifies for claims")
            priority_score += 20
        
        if land_size <= 2:
            reasons.append("✓ Small/marginal farmer benefits may apply")
            priority_score += 15
        
        # Damage-based prioritization
        if damage_percent >= 75:
            priority_score += 25
            reasons.append("✓ Severe damage (>75%) qualifies for maximum compensation")
        elif damage_percent >= 50:
            priority_score += 15
            reasons.append("✓ High damage (50-75%) eligible for substantial relief")
        
        # KCC holder benefits
        if has_kcc and 'kcc' in scheme.get('id', '').lower():
            priority_score += 30
            reasons.append("✓ KCC holder - eligible for loan restructuring benefits")
        
        # Estimate compensation based on damage
        max_amount = scheme.get('max_amount', 0)
        comp_percent = scheme.get('compensation_percent', 100)
        estimated_amount = int((max_amount * min(damage_percent, comp_percent)) / 100)
        
        eligible_schemes.append({
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
            'reasons': reasons,
            'priority_score': priority_score
        })
    
    # Sort by priority score (highest first)
    eligible_schemes.sort(key=lambda x: x.get('priority_score', 0), reverse=True)
    
    return eligible_schemes

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
            'daily_forecast': daily_forecasts
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
        'forecast': weather.get('daily_forecast', [])
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
    return render_template('voice_bot.html')

@app.route('/api/voice-bot', methods=['POST'])
@login_required
def voice_bot_api():
    """API endpoint for voice bot processing."""
    data = request.get_json()
    message = data.get('message', '').lower()
    language = data.get('language', 'en')
    
    # Simple intent detection and response generation
    response = generate_bot_response(message, language)
    
    return jsonify({'response': response})

def generate_bot_response(message, language):
    """Generate a response based on the user's message."""
    # Load translations for response
    translations = TRANSLATIONS_DATA.get('translations', {})
    
    # Helper to get translated text
    def tr(key):
        if key in translations:
            return translations[key].get(language, translations[key].get('en', key))
        return key
    
    message_lower = message.lower()
    
    # Weather related queries
    weather_keywords = ['weather', 'rain', 'forecast', 'mausam', 'barish', 'मौसम', 'बारिश', 'వాతావరణం', 'వర్షం', 'ಹವಾಮಾನ', 'ಮಳೆ', 'வானிலை', 'மழை', 'കാലാവസ്ഥ', 'മഴ']
    if any(kw in message_lower for kw in weather_keywords):
        return tr('bot_weather_response')
    
    # Crop related queries
    crop_keywords = ['crop', 'plant', 'sow', 'fasal', 'फसल', 'बोना', 'పంట', 'ವಿತ್ತನೆ', 'பயிர்', 'വിള', 'what to grow', 'which crop', 'best crop']
    if any(kw in message_lower for kw in crop_keywords):
        return tr('bot_crop_response')
    
    # Scheme related queries
    scheme_keywords = ['scheme', 'yojana', 'subsidy', 'government', 'help', 'sarkar', 'योजना', 'सरकार', 'సహాయం', 'పథకం', 'ಯೋಜನೆ', 'திட்டம்', 'പദ്ധതി']
    if any(kw in message_lower for kw in scheme_keywords):
        return tr('bot_scheme_response')
    
    # Price related queries
    price_keywords = ['price', 'mandi', 'rate', 'cost', 'daam', 'bhav', 'दाम', 'भाव', 'ధర', 'ಬೆಲೆ', 'விலை', 'വില', 'market']
    if any(kw in message_lower for kw in price_keywords):
        return tr('bot_price_response')
    
    # Pest related queries
    pest_keywords = ['pest', 'disease', 'insect', 'kida', 'rog', 'कीड़ा', 'रोग', 'పురుగు', 'ಕೀಟ', 'பூச்சி', 'കീടം']
    if any(kw in message_lower for kw in pest_keywords):
        return tr('bot_pest_response')
    
    # Greeting
    greeting_keywords = ['hello', 'hi', 'namaste', 'namaskar', 'नमस्ते', 'నమస్కారం', 'ನಮಸ್ಕಾರ', 'வணக்கம்', 'നമസ്കാരം']
    if any(kw in message_lower for kw in greeting_keywords):
        return tr('bot_greeting_response')
    
    # Thank you
    thanks_keywords = ['thank', 'dhanyavaad', 'shukriya', 'धन्यवाद', 'शुक्रिया', 'ధన్యవాదాలు', 'ಧನ್ಯವಾದ', 'நன்றி', 'നന്ദി']
    if any(kw in message_lower for kw in thanks_keywords):
        return tr('bot_thanks_response')
    
    # Default response
    return tr('bot_default_response')

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
    
    app.run(debug=True, host='0.0.0.0', port=5000)
