from google import genai
from google.genai import types
import wave

client = genai.Client()

# Try one of:
# "Sulafat (KK)"
# "Sulafat (RU)"
# "Sulafat (US)"
VOICE_NAME = "Sulafat (KK)"

response = client.models.generate_content(
    model="gemini-2.5-flash-preview-tts",
    contents="Сәлем! Бұл Sulafat дауысын тексеруге арналған тест.",
    config=types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=VOICE_NAME
                )
            )
        ),
    ),
)

audio_data = response.candidates[0].content.parts[0].inline_data.data

with open("output.wav", "wb") as f:
    f.write(audio_data)

print("Saved to output.wav")