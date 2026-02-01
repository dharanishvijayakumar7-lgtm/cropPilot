"""
Smart Farmer Assistant - Crop Advisor & Disaster Help Navigator
A Flask web application with user authentication, crop recommendations,
and disaster scheme finder.
"""

import os
import json
import sqlite3
import requests
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from functools import wraps
from model import load_model, predict_risk, train_model

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'your-secret-key-change-in-production')

# OpenWeather API configuration
OPENWEATHER_API_KEY = os.getenv('OPENWEATHER_API_KEY', 'YOUR_API_KEY_HERE')
OPENWEATHER_URL = "https://api.openweathermap.org/data/2.5/forecast"

# Load ML model and encoders
model, rainfall_encoder, season_encoder = load_model()

# Database helper functions
def get_db_connection():
    """Get SQLite database connection."""
    conn = sqlite3.connect('farmers.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize the database."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            state TEXT,
            district TEXT
        )
    """)
    conn.commit()
    conn.close()

# Initialize database on startup
init_db()

# Login required decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please login to access this page.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Load data files
def load_crops():
    """Load crop data from CSV file."""
    return pd.read_csv('crops.csv')

def load_crop_data():
    """Load crop data from JSON file."""
    with open('crop_data.json', 'r', encoding='utf-8') as f:
        return json.load(f)

def load_schemes_data():
    """Load schemes data from JSON file."""
    with open('schemes.json', 'r', encoding='utf-8') as f:
        return json.load(f)

def get_weather_forecast(lat, lon):
    """Fetch 5-day weather forecast from OpenWeather API."""
    params = {
        'lat': lat,
        'lon': lon,
        'appid': OPENWEATHER_API_KEY,
        'units': 'metric'
    }
    
    try:
        response = requests.get(OPENWEATHER_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        temps = []
        total_rainfall = 0
        
        for item in data.get('list', []):
            temps.append(item['main']['temp'])
            if 'rain' in item:
                total_rainfall += item['rain'].get('3h', 0)
        
        avg_temp = sum(temps) / len(temps) if temps else 25
        
        if total_rainfall < 10:
            rainfall_level = 'low'
        elif total_rainfall < 50:
            rainfall_level = 'medium'
        else:
            rainfall_level = 'high'
        
        return {
            'avg_temp': round(avg_temp, 1),
            'total_rainfall': round(total_rainfall, 1),
            'rainfall_level': rainfall_level,
            'success': True
        }
        
    except requests.RequestException as e:
        print(f"Weather API error: {e}")
        return {
            'avg_temp': 25.0,
            'total_rainfall': 0,
            'rainfall_level': 'medium',
            'success': False,
            'error': 'Could not fetch weather data. Using default values.'
        }

def filter_crops_by_conditions(crops_df, avg_temp, rainfall_level, season):
    """Filter crops based on weather and season conditions."""
    season_filtered = crops_df[crops_df['season'] == season].copy()
    
    temp_filtered = season_filtered[
        (season_filtered['min_temp'] <= avg_temp + 5) & 
        (season_filtered['max_temp'] >= avg_temp - 5)
    ].copy()
    
    if temp_filtered.empty:
        return season_filtered
    
    return temp_filtered

def get_crop_explanation(crop_row, avg_temp, rainfall_level, risk_score):
    """Generate explanation for why a crop is recommended."""
    explanations = []
    
    if crop_row['min_temp'] <= avg_temp <= crop_row['max_temp']:
        explanations.append(f"Temperature ({avg_temp}°C) is within optimal range ({crop_row['min_temp']}-{crop_row['max_temp']}°C)")
    else:
        explanations.append(f"Temperature is close to suitable range ({crop_row['min_temp']}-{crop_row['max_temp']}°C)")
    
    if crop_row['rainfall_need'] == rainfall_level:
        explanations.append(f"Rainfall level ({rainfall_level}) matches crop requirement")
    
    if crop_row['drought_tolerant'] == 1 and rainfall_level == 'low':
        explanations.append("This crop is drought-tolerant, suitable for low rainfall")
    
    if crop_row['flood_tolerant'] == 1 and rainfall_level == 'high':
        explanations.append("This crop is flood-tolerant, can handle high rainfall")
    
    if risk_score < 30:
        explanations.append("Low risk score indicates excellent growing conditions")
    elif risk_score < 60:
        explanations.append("Moderate risk score - good growing conditions expected")
    else:
        explanations.append("Higher risk score - monitor conditions carefully")
    
    return ". ".join(explanations) + "."

def recommend_crops(lat, lon, season):
    """Main recommendation logic combining weather, crop data, and ML model."""
    weather = get_weather_forecast(lat, lon)
    crops_df = load_crops()
    crop_data = load_crop_data()
    
    suitable_crops = filter_crops_by_conditions(
        crops_df, 
        weather['avg_temp'], 
        weather['rainfall_level'], 
        season
    )
    
    recommendations = []
    
    for _, crop in suitable_crops.iterrows():
        crop_temp = (crop['min_temp'] + crop['max_temp']) / 2
        temp_diff = abs(weather['avg_temp'] - crop_temp)
        
        base_risk = predict_risk(
            model, 
            rainfall_encoder, 
            season_encoder,
            weather['avg_temp'], 
            weather['rainfall_level'], 
            season
        )
        
        adjusted_risk = base_risk
        
        if weather['avg_temp'] < crop['min_temp'] or weather['avg_temp'] > crop['max_temp']:
            adjusted_risk += min(temp_diff * 2, 20)
        
        if weather['rainfall_level'] == 'low' and crop['drought_tolerant'] == 1:
            adjusted_risk -= 10
        elif weather['rainfall_level'] == 'high' and crop['flood_tolerant'] == 1:
            adjusted_risk -= 10
        elif weather['rainfall_level'] != crop['rainfall_need']:
            adjusted_risk += 10
        
        adjusted_risk = max(0, min(100, adjusted_risk))
        
        explanation = get_crop_explanation(
            crop, 
            weather['avg_temp'], 
            weather['rainfall_level'], 
            adjusted_risk
        )
        
        # Get growing days from JSON data
        growing_days = 90  # default
        for crop_info in crop_data.get('crops', []):
            if crop_info['name'] == crop['crop']:
                growing_days = crop_info.get('growing_days', 90)
                break
        
        recommendations.append({
            'name': crop['crop'],
            'risk_score': int(adjusted_risk),
            'min_temp': crop['min_temp'],
            'max_temp': crop['max_temp'],
            'rainfall_need': crop['rainfall_need'],
            'drought_tolerant': bool(crop['drought_tolerant']),
            'flood_tolerant': bool(crop['flood_tolerant']),
            'growing_days': growing_days,
            'explanation': explanation
        })
    
    recommendations.sort(key=lambda x: x['risk_score'])
    top_recommendations = recommendations[:3]
    
    return {
        'weather': weather,
        'season': season,
        'location': {'lat': lat, 'lon': lon},
        'recommendations': top_recommendations,
        'total_crops_analyzed': len(suitable_crops)
    }

def find_eligible_schemes(crop, disaster_type, land_size, has_insurance):
    """Find eligible government schemes based on user inputs."""
    schemes_data = load_schemes_data()
    eligible_schemes = []
    
    for scheme in schemes_data['schemes']:
        # Check disaster type
        if disaster_type not in scheme['disaster_types']:
            continue
        
        # Check crop eligibility
        if crop not in scheme['eligible_crops']:
            continue
        
        # Check land size
        if land_size < scheme['min_land_size'] or land_size > scheme['max_land_size']:
            continue
        
        # Check insurance requirement
        if scheme['requires_insurance'] and not has_insurance:
            continue
        
        # Build eligibility reasons
        reasons = []
        reasons.append(f"Your crop ({crop}) is covered under this scheme")
        reasons.append(f"Disaster type ({disaster_type}) is eligible")
        reasons.append(f"Your land size ({land_size} hectares) meets the criteria")
        if has_insurance and 'insurance' in scheme['id']:
            reasons.append("You have crop insurance which qualifies for claims")
        if land_size <= 2:
            reasons.append("Small/marginal farmer benefits apply")
        
        eligible_schemes.append({
            'name': scheme['name'],
            'description': scheme['description'],
            'max_amount': scheme['max_amount'],
            'documents': scheme['documents_required'],
            'steps': scheme['application_steps'],
            'helpline': scheme['helpline'],
            'website': scheme['website'],
            'reasons': reasons
        })
    
    return eligible_schemes

# ==================== ROUTES ====================

@app.route('/')
def index():
    """Redirect to login or dashboard."""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Handle user login."""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        phone = request.form.get('phone', '').strip()
        password = request.form.get('password', '')
        input_name = request.form.get('name', '').strip()  # Get name from form

        if not phone or not password:
            flash('Please enter phone number and password.', 'danger')
            return render_template('login.html')

        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE phone = ?', (phone,)).fetchone()
        conn.close()

        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            # Use input name if provided, otherwise use registered name
            session['user_name'] = input_name if input_name else user['name']
            session['user_phone'] = user['phone']
            session['user_state'] = user['state']
            session['user_district'] = user['district']
            
            # Personalized welcome message
            display_name = input_name if input_name else user['name']
            flash(f'🙏 Namaste, {display_name}! Welcome back!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid phone number or password.', 'danger')

    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    """Handle user registration."""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    
    schemes_data = load_schemes_data()
    states = schemes_data.get('states', [])
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        phone = request.form.get('phone', '').strip()
        state = request.form.get('state', '').strip()
        district = request.form.get('district', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        # Validation
        if not all([name, phone, state, district, password]):
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
        existing_user = conn.execute('SELECT id FROM users WHERE phone = ?', (phone,)).fetchone()
        
        if existing_user:
            conn.close()
            flash('Phone number already registered. Please login.', 'warning')
            return redirect(url_for('login'))
        
        # Create new user
        password_hash = generate_password_hash(password)
        cursor = conn.execute(
            'INSERT INTO users (name, phone, password_hash, state, district) VALUES (?, ?, ?, ?, ?)',
            (name, phone, password_hash, state, district)
        )
        conn.commit()
        user_id = cursor.lastrowid
        conn.close()
        
        # Auto-login after registration
        session['user_id'] = user_id
        session['user_name'] = name
        session['user_phone'] = phone
        session['user_state'] = state
        session['user_district'] = district
        
        flash('Registration successful! Welcome to Smart Farmer Assistant.', 'success')
        return redirect(url_for('dashboard'))
    
    return render_template('register.html', states=states)

@app.route('/logout')
def logout():
    """Handle user logout."""
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    """Render the main dashboard."""
    return render_template('dashboard.html')

@app.route('/crop-advisor')
@login_required
def crop_form():
    """Render the crop advisory form."""
    crop_data = load_crop_data()
    seasons = crop_data.get('seasons', {})
    return render_template('crop_form.html', seasons=seasons)

@app.route('/crop-result', methods=['POST'])
@login_required
def crop_result():
    """Process crop advisory form and show recommendations."""
    try:
        lat = float(request.form.get('latitude', 0))
        lon = float(request.form.get('longitude', 0))
        season = request.form.get('season', 'Kharif')
        
        if not (-90 <= lat <= 90):
            flash('Latitude must be between -90 and 90.', 'danger')
            return redirect(url_for('crop_form'))
        
        if not (-180 <= lon <= 180):
            flash('Longitude must be between -180 and 180.', 'danger')
            return redirect(url_for('crop_form'))
        
        results = recommend_crops(lat, lon, season)
        return render_template('crop_result.html', results=results)
        
    except ValueError:
        flash('Please enter valid numeric values for coordinates.', 'danger')
        return redirect(url_for('crop_form'))
    except Exception as e:
        print(f"Error: {e}")
        flash('An error occurred. Please try again.', 'danger')
        return redirect(url_for('crop_form'))

@app.route('/disaster-help')
@login_required
def disaster_form():
    """Render the disaster help form."""
    schemes_data = load_schemes_data()
    crop_data = load_crop_data()
    
    crops = [crop['name'] for crop in crop_data.get('crops', [])]
    disaster_types = schemes_data.get('disaster_types', [])
    
    return render_template('disaster_form.html', crops=crops, disaster_types=disaster_types)

@app.route('/disaster-result', methods=['POST'])
@login_required
def disaster_result():
    """Process disaster form and show eligible schemes."""
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
            (d for d in schemes_data['disaster_types'] if d['id'] == disaster_type),
            {'id': disaster_type, 'name': disaster_type, 'icon': '⚠️'}
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
        print(f"Error: {e}")
        flash('An error occurred. Please try again.', 'danger')
        return redirect(url_for('disaster_form'))

# Ensure model is trained before first request
@app.before_request
def ensure_model():
    """Ensure ML model is available."""
    global model, rainfall_encoder, season_encoder
    if model is None:
        model, rainfall_encoder, season_encoder = load_model()

if __name__ == '__main__':
    # Train model if it doesn't exist
    if not os.path.exists('model.pkl'):
        print("Training ML model for first time...")
        train_model()
    
    print("Starting Smart Farmer Assistant...")
    print("Open http://127.0.0.1:5000 in your browser")
    app.run(debug=True, host='0.0.0.0', port=5000)
