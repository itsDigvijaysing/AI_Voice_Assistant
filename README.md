# AI Voice Assistant

This project is a simple AI voice assistant that captures audio input, transcribes it to text, generates a response using an AI language model, and converts the response back to speech.

## Features

- **Speech Recognition**: Captures audio from the user's microphone and transcribes it using the Whisper model.
- **Language Model Interaction**: Generates responses to user input using the Ollama language model integrated via LangChain.
- **Text-to-Speech**: Converts the AI-generated response back to speech and plays it using the `TextToSpeechService`.
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