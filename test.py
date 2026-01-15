import google.generativeai as genai
genai.configure(api_key="AIzaSyDH4uHsaqSsWHhUSg-HTgiLJls13vyYJJk")
model = genai.GenerativeModel("gemini-2.0-flash")
resp = model.generate_content("Say hello")
print(resp.text)
