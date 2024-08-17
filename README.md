# AI Voice Assistant

This project is a simple AI voice assistant that captures audio input, transcribes it to text, generates a response using an AI language model, and converts the response back to speech.

## Features

- **Speech Recognition**: Captures audio from the user's microphone and transcribes it using the Whisper model.
- **Language Model Interaction**: Generates responses to user input using the Llama-2 language model integrated via LangChain.
- **Text-to-Speech**: Converts the AI-generated response back to speech and plays it using the TextToSpeechService.
- **Rich Console Output**: Displays the conversation in the console with styled text for better readability.

## Requirements

To run this project, you need the following Python packages:

- `time`
- `threading`
- `numpy`
- `whisper`
- `sounddevice`
- `queue`
- `rich`
- `langchain`
- `langchain_community`
- `tts`

You can install these dependencies using `pip`:

```bash
pip install numpy whisper rich langchain langchain_community tts
```

## Usage

1. **Start the Assistant**: Run the script to start the assistant.
2. **Record Audio**: Press `Enter` to start recording your voice. Press `Enter` again to stop recording.
3. **Transcription**: The assistant transcribes your voice to text.
4. **AI Response**: The transcribed text is sent to an AI language model (Llama-2) to generate a response.
5. **Text-to-Speech**: The AI response is then converted to speech and played back to you.
6. **Repeat**: You can repeat the process by pressing `Enter` again.

## How It Works

1. **Recording Audio**: The assistant captures audio input using the `sounddevice` library in a separate thread.
2. **Transcription**: The audio is transcribed to text using the `Whisper` model from OpenAI.
3. **LLM Response**: The transcribed text is passed to a language model (Llama-2) using LangChain to generate a response.
4. **Playback**: The response is converted to speech using a text-to-speech service and played back to the user.

## Code Overview

- `record_audio(stop_event, data_queue)`: Captures audio data and adds it to a queue.
- `transcribe(audio_np)`: Transcribes audio data to text using Whisper.
- `get_llm_response(text)`: Generates an AI response using the Llama-2 language model.
- `play_audio(sample_rate, audio_array)`: Plays audio data using `sounddevice`.
- `main`: Handles the main loop for recording, transcribing, generating responses, and playing audio.

## Example

```bash
python assistant.py
```

Start the assistant and follow the prompts to record your voice and interact with the AI.

## Acknowledgments

- **Whisper**: Used for speech-to-text conversion.
- **LangChain**: Used to interact with the Llama-2 language model.
- **TextToSpeechService**: Custom service for text-to-speech conversion.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
