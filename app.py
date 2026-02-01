"""
CropPilot - Intelligent Crop Recommendation System
A Flask web application that recommends suitable crops based on
location, season, and live weather data.
"""

import os
import requests
import pandas as pd
from flask import Flask, render_template, request
from dotenv import load_dotenv
from model import load_model, predict_risk, train_model

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# OpenWeather API configuration
# Replace with your API key or set in .env file
OPENWEATHER_API_KEY = os.getenv('OPENWEATHER_API_KEY', 'YOUR_API_KEY_HERE')
OPENWEATHER_URL = "https://api.openweathermap.org/data/2.5/forecast"

# Load ML model and encoders
model, rainfall_encoder, season_encoder = load_model()

# Load crop dataset
def load_crops():
    """Load crop data from CSV file."""
    return pd.read_csv('crops.csv')


def get_weather_forecast(lat, lon):
    """
    Fetch 5-day weather forecast from OpenWeather API.
    
    Args:
        lat: Latitude
        lon: Longitude
    
    Returns:
        dict: Weather summary with avg_temp and rainfall_level
    """
    params = {
        'lat': lat,
        'lon': lon,
        'appid': OPENWEATHER_API_KEY,
        'units': 'metric'  # Celsius
    }
    
    try:
        response = requests.get(OPENWEATHER_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # Extract temperature and rainfall data
        temps = []
        total_rainfall = 0
        
        for item in data.get('list', []):
            temps.append(item['main']['temp'])
            # Rainfall in last 3 hours (if available)
            if 'rain' in item:
                total_rainfall += item['rain'].get('3h', 0)
        
        # Calculate average temperature
        avg_temp = sum(temps) / len(temps) if temps else 25
        
        # Determine rainfall level
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
        # Return default values if API fails
        return {
            'avg_temp': 25.0,
            'total_rainfall': 0,
            'rainfall_level': 'medium',
            'success': False,
            'error': 'Could not fetch weather data. Using default values.'
        }


def filter_crops_by_conditions(crops_df, avg_temp, rainfall_level, season):
    """
    Filter crops based on weather and season conditions.
    
    Args:
        crops_df: DataFrame with crop data
        avg_temp: Average temperature
        rainfall_level: 'low', 'medium', or 'high'
        season: 'Kharif', 'Rabi', or 'Zaid'
    
    Returns:
        DataFrame: Filtered crops that match conditions
    """
    # Filter by season
    season_filtered = crops_df[crops_df['season'] == season].copy()
    
    # Filter by temperature range (with some tolerance)
    temp_filtered = season_filtered[
        (season_filtered['min_temp'] <= avg_temp + 5) & 
        (season_filtered['max_temp'] >= avg_temp - 5)
    ].copy()
    
    # If no crops match, return season-filtered crops
    if temp_filtered.empty:
        return season_filtered
    
    return temp_filtered


def get_crop_explanation(crop_row, avg_temp, rainfall_level, risk_score):
    """
    Generate explanation for why a crop is recommended.
    
    Args:
        crop_row: Series with crop data
        avg_temp: Average temperature
        rainfall_level: Rainfall level
        risk_score: Predicted risk score
    
    Returns:
        str: Explanation text
    """
    explanations = []
    
    # Temperature suitability
    if crop_row['min_temp'] <= avg_temp <= crop_row['max_temp']:
        explanations.append(f"Temperature ({avg_temp}°C) is within optimal range ({crop_row['min_temp']}-{crop_row['max_temp']}°C)")
    else:
        explanations.append(f"Temperature is close to suitable range ({crop_row['min_temp']}-{crop_row['max_temp']}°C)")
    
    # Rainfall suitability
    if crop_row['rainfall_need'] == rainfall_level:
        explanations.append(f"Rainfall level ({rainfall_level}) matches crop requirement")
    
    # Drought tolerance
    if crop_row['drought_tolerant'] == 1 and rainfall_level == 'low':
        explanations.append("This crop is drought-tolerant, suitable for low rainfall")
    
    # Flood tolerance
    if crop_row['flood_tolerant'] == 1 and rainfall_level == 'high':
        explanations.append("This crop is flood-tolerant, can handle high rainfall")
    
    # Risk assessment
    if risk_score < 30:
        explanations.append("Low risk score indicates excellent growing conditions")
    elif risk_score < 60:
        explanations.append("Moderate risk score - good growing conditions expected")
    else:
        explanations.append("Higher risk score - monitor conditions carefully")
    
    return ". ".join(explanations) + "."


def recommend_crops(lat, lon, season):
    """
    Main recommendation logic combining weather, crop data, and ML model.
    
    Args:
        lat: Latitude
        lon: Longitude
        season: Selected season
    
    Returns:
        dict: Recommendation results
    """
    # Get weather forecast
    weather = get_weather_forecast(lat, lon)
    
    # Load crop data
    crops_df = load_crops()
    
    # Filter crops by conditions
    suitable_crops = filter_crops_by_conditions(
        crops_df, 
        weather['avg_temp'], 
        weather['rainfall_level'], 
        season
    )
    
    # Calculate risk scores for each crop
    recommendations = []
    
    for _, crop in suitable_crops.iterrows():
        # Adjust temperature based on crop's optimal range
        crop_temp = (crop['min_temp'] + crop['max_temp']) / 2
        temp_diff = abs(weather['avg_temp'] - crop_temp)
        
        # Get base risk from ML model
        base_risk = predict_risk(
            model, 
            rainfall_encoder, 
            season_encoder,
            weather['avg_temp'], 
            weather['rainfall_level'], 
            season
        )
        
        # Adjust risk based on specific crop factors
        adjusted_risk = base_risk
        
        # Temperature adjustment
        if weather['avg_temp'] < crop['min_temp'] or weather['avg_temp'] > crop['max_temp']:
            adjusted_risk += min(temp_diff * 2, 20)
        
        # Rainfall adjustment
        if weather['rainfall_level'] == 'low' and crop['drought_tolerant'] == 1:
            adjusted_risk -= 10
        elif weather['rainfall_level'] == 'high' and crop['flood_tolerant'] == 1:
            adjusted_risk -= 10
        elif weather['rainfall_level'] != crop['rainfall_need']:
            adjusted_risk += 10
        
        # Clamp risk score
        adjusted_risk = max(0, min(100, adjusted_risk))
        
        # Generate explanation
        explanation = get_crop_explanation(
            crop, 
            weather['avg_temp'], 
            weather['rainfall_level'], 
            adjusted_risk
        )
        
        recommendations.append({
            'crop': crop['crop'],
            'risk_score': int(adjusted_risk),
            'min_temp': crop['min_temp'],
            'max_temp': crop['max_temp'],
            'rainfall_need': crop['rainfall_need'],
            'drought_tolerant': bool(crop['drought_tolerant']),
            'flood_tolerant': bool(crop['flood_tolerant']),
            'explanation': explanation
        })
    
    # Sort by risk score (lower is better) and get top 3
    recommendations.sort(key=lambda x: x['risk_score'])
    top_recommendations = recommendations[:3]
    
    return {
        'weather': weather,
        'season': season,
        'location': {'lat': lat, 'lon': lon},
        'recommendations': top_recommendations,
        'total_crops_analyzed': len(suitable_crops)
    }


@app.route('/')
def index():
    """Render the home page with input form."""
    return render_template('index.html')


@app.route('/result', methods=['POST'])
def result():
    """Process form submission and show recommendations."""
    try:
        # Get form data
        lat = float(request.form.get('latitude', 0))
        lon = float(request.form.get('longitude', 0))
        season = request.form.get('season', 'Kharif')
        
        # Validate inputs
        if not (-90 <= lat <= 90):
            return render_template('index.html', error="Latitude must be between -90 and 90")
        if not (-180 <= lon <= 180):
            return render_template('index.html', error="Longitude must be between -180 and 180")
        
        # Get recommendations
        results = recommend_crops(lat, lon, season)
        
        return render_template('result.html', results=results)
        
    except ValueError as e:
        return render_template('index.html', error="Please enter valid numeric values for coordinates")
    except Exception as e:
        print(f"Error: {e}")
        return render_template('index.html', error="An error occurred. Please try again.")


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
    
    # Run the Flask app
    print("Starting CropPilot...")
    print("Open http://127.0.0.1:5000 in your browser")
    app.run(debug=True, host='0.0.0.0', port=5000)
