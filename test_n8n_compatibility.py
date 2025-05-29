#!/usr/bin/env python3
"""
Test script to verify N8N compatibility with the watsonx OpenAI API gateway.
This script simulates how N8N would call the API with an API key.
"""

import requests
import json

# Configuration
BASE_URL = "http://localhost:8080"
FAKE_API_KEY = "sk-fake-api-key-for-n8n-compatibility"

def test_models_endpoint():
    """Test the /v1/models endpoint with authorization header"""
    print("Testing /v1/models endpoint...")
    
    headers = {
        "Authorization": f"Bearer {FAKE_API_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.get(f"{BASE_URL}/v1/models", headers=headers)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            models = response.json()
            print(f"✅ Models endpoint working! Found {len(models['data'])} models")
            print(f"First model: {models['data'][0]['id']}")
        else:
            print(f"❌ Error: {response.text}")
    except Exception as e:
        print(f"❌ Connection error: {e}")

def test_chat_completions():
    """Test the /v1/chat/completions endpoint with authorization header"""
    print("\nTesting /v1/chat/completions endpoint...")
    
    headers = {
        "Authorization": f"Bearer {FAKE_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "ibm/granite-20b-multilingual",
        "messages": [
            {
                "role": "user", 
                "content": "Say hello in Spanish"
            }
        ],
        "max_tokens": 100,
        "temperature": 0.7
    }
    
    try:
        response = requests.post(f"{BASE_URL}/v1/chat/completions", 
                               headers=headers, 
                               json=payload)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            print("✅ Chat completions endpoint working!")
            print(f"Response: {result}")
        else:
            print(f"❌ Error: {response.text}")
    except Exception as e:
        print(f"❌ Connection error: {e}")

def test_without_auth():
    """Test that endpoints also work without authorization header"""
    print("\nTesting without authorization header...")
    
    headers = {
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.get(f"{BASE_URL}/v1/models", headers=headers)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            print("✅ Works without auth too!")
        else:
            print(f"❌ Error without auth: {response.text}")
    except Exception as e:
        print(f"❌ Connection error: {e}")

if __name__ == "__main__":
    print("🧪 Testing N8N Compatibility with watsonx OpenAI Gateway")
    print("=" * 60)
    
    test_models_endpoint()
    test_chat_completions()
    test_without_auth()
    
    print("\n" + "=" * 60)
    print("🎯 Configuration for N8N:")
    print(f"   Base URL: {BASE_URL}/v1")
    print(f"   API Key: {FAKE_API_KEY} (or any string)")
    print("   Model: ibm/granite-20b-multilingual (or any watsonx model)") 