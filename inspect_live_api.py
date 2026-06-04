import requests
import json

def test_live_api():
    url = "https://swanweather-production.up.railway.app/weather/full"
    params = {
        "latitude": 19.0,
        "longitude": 73.0
    }
    
    print(f"GET Request to: {url} with params: {params}")
    try:
        response = requests.get(url, params=params, timeout=30)
        print("Status Code:", response.status_code)
        print("Headers:", dict(response.headers))
        try:
            print("Response JSON:", json.dumps(response.json(), indent=2))
        except Exception:
            print("Response Text (not JSON):", response.text)
    except Exception as e:
        print("Request failed with exception:", e)

if __name__ == "__main__":
    test_live_api()
