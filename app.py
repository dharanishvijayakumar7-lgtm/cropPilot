"""
CropPilot - Farmer Decision Support System
A Flask web application with login, weather risk map, farm logbook, and disaster scheme navigator.
"""

import os
import json
import sqlite3
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from functools import wraps

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'your-secret-key-change-in-production')

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

def find_eligible_schemes(crop, disaster_type, land_size, has_insurance):
    """Find eligible government schemes based on farmer inputs."""
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
        
        # Build eligibility reasons
        reasons = []
        reasons.append(f"Your crop ({crop}) is covered under this scheme")
        reasons.append(f"Disaster type ({disaster_type.replace('_', ' ').title()}) is eligible")
        reasons.append(f"Your land size ({land_size} hectares) meets the criteria")
        if has_insurance and scheme.get('requires_insurance'):
            reasons.append("You have crop insurance which qualifies for claims")
        if land_size <= 2:
            reasons.append("Small/marginal farmer benefits may apply")
        
        eligible_schemes.append({
            'id': scheme.get('id'),
            'name': scheme.get('name'),
            'description': scheme.get('description'),
            'max_amount': scheme.get('max_amount', 'Varies'),
            'documents': scheme.get('documents_required', []),
            'steps': scheme.get('application_steps', []),
            'helpline': scheme.get('helpline', 'N/A'),
            'website': scheme.get('website', '#'),
            'reasons': reasons
        })
    
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
        
        for item in data.get('list', []):
            temp_values.append(item['main']['temp'])
            humidity_values.append(item['main'].get('humidity', 50))
            if 'rain' in item:
                total_rainfall += item['rain'].get('3h', 0)
        
        avg_humidity = sum(humidity_values) / len(humidity_values) if humidity_values else 50
        avg_temp = sum(temp_values) / len(temp_values) if temp_values else 25
        city_name = data.get('city', {}).get('name', 'Unknown Location')
        
        return {
            'success': True,
            'total_rainfall': round(total_rainfall, 2),
            'avg_humidity': round(avg_humidity, 1),
            'avg_temp': round(avg_temp, 1),
            'city': city_name,
            'forecast_days': 5
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
    estimated_10day_rainfall = total_rainfall * 2
    
    if estimated_10day_rainfall < 10:
        risk_level = 'drought'
        risk_color = '#e74c3c'
        risk_title = 'High Drought Risk'
        risk_message = f"This village is expected to receive very low rainfall (estimated {estimated_10day_rainfall:.1f}mm in next 10 days). High drought risk. Consider water conservation measures and drought-resistant crops."
    elif estimated_10day_rainfall < 50 and avg_humidity < 70:
        risk_level = 'moderate'
        risk_color = '#f39c12'
        risk_title = 'Moderate Conditions'
        risk_message = f"This village has moderate weather conditions. Expected rainfall: {estimated_10day_rainfall:.1f}mm. Humidity: {avg_humidity:.1f}%. Good conditions for most crops."
    elif estimated_10day_rainfall >= 50 or avg_humidity >= 80:
        risk_level = 'excess_moisture'
        risk_color = '#3498db'
        risk_title = 'Excess Moisture Risk'
        risk_message = f"This village may experience excess moisture. Expected rainfall: {estimated_10day_rainfall:.1f}mm. Humidity: {avg_humidity:.1f}%. Risk of flooding or pest/fungal issues. Ensure proper drainage."
    else:
        risk_level = 'moderate'
        risk_color = '#f39c12'
        risk_title = 'Moderate Conditions'
        risk_message = f"Expected rainfall: {estimated_10day_rainfall:.1f}mm. Humidity: {avg_humidity:.1f}%. Normal conditions expected."
    
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
            'avg_temp': avg_temp
        }
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

# ==================== DISASTER SCHEME NAVIGATOR ====================

@app.route('/disaster-help')
@login_required
def disaster_form():
    """Disaster Scheme Navigator - Input form."""
    schemes_data = load_schemes_data()
    crops = schemes_data.get('crops', [])
    disaster_types = schemes_data.get('disaster_types', [])
    return render_template('disaster_form.html', crops=crops, disaster_types=disaster_types)

@app.route('/disaster-result', methods=['POST'])
@login_required
def disaster_result():
    """Disaster Scheme Navigator - Results page."""
    try:
        crop = request.form.get('crop', '')
        disaster_type = request.form.get('disaster_type', '')
        land_size = float(request.form.get('land_size', 0))
        has_insurance = request.form.get('has_insurance', 'no') == 'yes'
        
        if not crop or not disaster_type or land_size <= 0:
            flash('Please fill all required fields.', 'danger')
            return redirect(url_for('disaster_form'))
        
        # Get disaster info for display
        schemes_data = load_schemes_data()
        disaster_info = next(
            (d for d in schemes_data.get('disaster_types', []) if d['id'] == disaster_type),
            {'id': disaster_type, 'name': disaster_type.replace('_', ' ').title(), 'icon': '⚠️'}
        )
        
        # Find eligible schemes
        schemes = find_eligible_schemes(crop, disaster_type, land_size, has_insurance)
        
        return render_template(
            'disaster_result.html',
            crop=crop,
            disaster_info=disaster_info,
            land_size=land_size,
            has_insurance=has_insurance,
            schemes=schemes
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
