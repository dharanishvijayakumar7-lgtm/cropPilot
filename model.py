"""
CropPilot ML Model
Trains a RandomForestClassifier to predict crop risk scores
based on temperature, rainfall, and season.
"""

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import LabelEncoder
import joblib
import os


def generate_training_data(n_samples=1000):
    """
    Generate synthetic training data for crop risk prediction.
    In production, this would be replaced with historical agricultural data.
    """
    np.random.seed(42)
    
    # Generate random features
    temperatures = np.random.uniform(5, 40, n_samples)
    rainfall_levels = np.random.choice(['low', 'medium', 'high'], n_samples)
    seasons = np.random.choice(['Kharif', 'Rabi', 'Zaid'], n_samples)
    
    # Create risk scores based on rules (simulating real-world patterns)
    risk_scores = []
    for temp, rain, season in zip(temperatures, rainfall_levels, seasons):
        risk = 50  # Base risk
        
        # Temperature factors
        if temp < 10 or temp > 35:
            risk += 20  # Extreme temperatures increase risk
        elif 20 <= temp <= 30:
            risk -= 15  # Optimal temperature range
        
        # Rainfall factors
        if rain == 'low':
            risk += 10 if season == 'Kharif' else -5
        elif rain == 'high':
            risk += 15 if season == 'Rabi' else -10
        
        # Season factors
        if season == 'Zaid':
            risk += 5  # Summer crops slightly riskier
        
        # Add some noise
        risk += np.random.normal(0, 10)
        
        # Clamp to 0-100 range
        risk = max(0, min(100, risk))
        risk_scores.append(int(risk))
    
    return pd.DataFrame({
        'temperature': temperatures,
        'rainfall_level': rainfall_levels,
        'season': seasons,
        'risk_score': risk_scores
    })


def train_model():
    """
    Train the RandomForest model and save it to disk.
    """
    print("Generating training data...")
    df = generate_training_data(1000)
    
    # Encode categorical variables
    rainfall_encoder = LabelEncoder()
    season_encoder = LabelEncoder()
    
    df['rainfall_encoded'] = rainfall_encoder.fit_transform(df['rainfall_level'])
    df['season_encoded'] = season_encoder.fit_transform(df['season'])
    
    # Prepare features and target
    X = df[['temperature', 'rainfall_encoded', 'season_encoded']]
    y = df['risk_score']
    
    # Train RandomForest model
    print("Training RandomForest model...")
    model = RandomForestClassifier(
        n_estimators=100,
        max_depth=10,
        random_state=42,
        n_jobs=-1
    )
    model.fit(X, y)
    
    # Save model and encoders
    print("Saving model and encoders...")
    joblib.dump(model, 'model.pkl')
    joblib.dump(rainfall_encoder, 'rainfall_encoder.pkl')
    joblib.dump(season_encoder, 'season_encoder.pkl')
    
    print("Model training complete!")
    return model, rainfall_encoder, season_encoder


def load_model():
    """
    Load the trained model and encoders from disk.
    If not found, train a new model.
    """
    if not os.path.exists('model.pkl'):
        print("Model not found. Training new model...")
        return train_model()
    
    model = joblib.load('model.pkl')
    rainfall_encoder = joblib.load('rainfall_encoder.pkl')
    season_encoder = joblib.load('season_encoder.pkl')
    
    return model, rainfall_encoder, season_encoder


def predict_risk(model, rainfall_encoder, season_encoder, temperature, rainfall_level, season):
    """
    Predict the risk score for given conditions.
    
    Args:
        model: Trained RandomForest model
        rainfall_encoder: Fitted LabelEncoder for rainfall
        season_encoder: Fitted LabelEncoder for season
        temperature: Average temperature in Celsius
        rainfall_level: 'low', 'medium', or 'high'
        season: 'Kharif', 'Rabi', or 'Zaid'
    
    Returns:
        int: Risk score (0-100, lower is better)
    """
    # Handle unseen labels gracefully
    try:
        rainfall_encoded = rainfall_encoder.transform([rainfall_level])[0]
    except ValueError:
        rainfall_encoded = 1  # Default to medium
    
    try:
        season_encoded = season_encoder.transform([season])[0]
    except ValueError:
        season_encoded = 0  # Default
    
    # Create feature array
    features = np.array([[temperature, rainfall_encoded, season_encoded]])
    
    # Predict risk score
    risk_score = model.predict(features)[0]
    
    return int(risk_score)


# Train model if this file is run directly
if __name__ == "__main__":
    train_model()