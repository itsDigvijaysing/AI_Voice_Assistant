# AI Voice Assistant with Ollama Models

## Overview

This project is an AI Voice Assistant that leverages the Ollama models to provide concise and helpful responses. The assistant uses Whisper for speech-to-text transcription and a custom Text-to-Speech (TTS) service to convert the responses back into audio. The assistant is designed to interact with users via voice commands, making it easy to receive information or answers to questions through a natural conversation.

## Features

- **Speech Recognition**: Utilizes Whisper for real-time speech-to-text transcription.
- **Conversational AI**: Employs Ollama models for generating intelligent responses based on the input text.
- **Text-to-Speech**: Converts AI-generated responses back to speech using a custom TTS service.
- **User Interaction**: Allows users to record their queries and receive spoken responses, creating a seamless voice interaction experience.
- **Rich Console Interface**: Provides feedback and status updates using the Rich library for a visually enhanced console interface.

## Installation

1. **Clone the repository:**
   ```bash
   git clone <repository-url>
   cd <repository-directory>
   ```

2. **Install the required Python packages:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Install the necessary models:**
   - Whisper model: `"base.en"`
   - Ollama LLM: Make sure the Ollama package is correctly configured in your environment.

4. **Ensure your microphone is working**: This application requires access to your microphone to capture audio.

## Usage

1. **Start the Assistant:**
   ```bash
   python app.py
   ```

2. **Interaction:**
   - Press `Enter` to start recording your query.
   - Press `Enter` again to stop recording.
   - The assistant will transcribe your voice input, generate a response using the Ollama model, and then play back the response via TTS.

3. **Stop the Assistant:**
   - Press `Ctrl+C` to exit the application gracefully.

## How It Works

1. **Audio Recording**: 
   - The assistant captures audio input from the user's microphone and stores it in a queue.

2. **Speech Transcription**:
   - The recorded audio is transcribed into text using the Whisper model.

3. **Response Generation**:
   - The transcribed text is fed into the Ollama model to generate an AI response.

4. **Text-to-Speech**:
   - The generated response is converted into speech using the TTS service and played back to the user.

5. **Console Feedback**:
   - The console displays real-time feedback, including the transcribed text and generated responses, with status updates for transcription and processing.

## Dependencies

- `whisper`: For speech-to-text transcription.
- `sounddevice`: To capture audio from the microphone and play back audio.
- `numpy`: For handling audio data.
- `rich`: For enhanced console output.
- `langchain`: For conversational AI and memory management.
- `queue`: For handling audio data in a multi-threaded environment.
- `threading`: For managing concurrent audio recording.

## Future Improvements

- **Enhanced TTS**: Integrate a more advanced TTS service to improve the naturalness and clarity of the generated speech.
- **Custom Prompts**: Allow users to define custom prompt templates for different interaction contexts.
- **Multi-language Support**: Extend the assistant's capabilities to support multiple languages for both input and output.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## Acknowledgments

- Thanks to the creators of Whisper, Ollama, and other open-source libraries used in this project.
- Special appreciation to the developers and contributors of the Rich and Sounddevice libraries.