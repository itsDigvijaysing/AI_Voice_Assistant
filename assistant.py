import time
import threading
import numpy as np
import whisper
import sounddevice as sd
from queue import Queue
from rich.console import Console
from langchain.memory import ConversationBufferMemory
from langchain.chains import ConversationChain
from langchain.prompts import PromptTemplate
from langchain_community.llms import Ollama
from tts import TextToSpeechService

# Libraries used:
# - time: For handling time-related tasks such as sleeping.
# - threading: For running tasks in separate threads.
# - numpy: For handling numerical data, especially arrays.
# - whisper: For speech-to-text conversion.
# - sounddevice: For capturing and playing audio.
# - queue: For managing a queue of audio data.
# - rich.console: For rich text output in the console.
# - langchain.memory: For managing conversation memory.
# - langchain.chains: For creating conversation chains.
# - langchain.prompts: For defining prompt templates.
# - langchain_community.llms: For using the Ollama language model.
# - tts: For text-to-speech conversion.


console = Console()
stt = whisper.load_model("base.en")
tts = TextToSpeechService()

# Template for conversation transcript and user's follow-up
template = """
You are an AI assistant, striving to be helpful, friendly, and provide concise responses within 20 words.
Here is our conversation transcript:
{history}

And here is the user's follow-up: {input}

Please provide your response:
"""
PROMPT = PromptTemplate(input_variables=["history", "input"], template=template)
chain = ConversationChain(
    prompt=PROMPT,
    verbose=False,
    memory=ConversationBufferMemory(ai_prefix="Assistant:"),
    llm=Ollama(),
)


def record_audio(stop_event, data_queue):
    """
    Captures audio data from the user's microphone and adds it to a queue for further processing.

    Args:
        stop_event (threading.Event): An event that, when set, signals the function to stop recording.
        data_queue (queue.Queue): A queue to which the recorded audio data will be added.

    Returns:
        None
    """
    def callback(indata, frames, time, status):
        if status:
            console.print(status)
        data_queue.put(bytes(indata))

    with sd.RawInputStream(
        samplerate=16000, dtype="int16", channels=1, callback=callback
    ):
        while not stop_event.is_set():
            time.sleep(0.1)


def transcribe(audio_np: np.ndarray) -> str:
    """
    Transcribes the given audio data using the Whisper speech recognition model.

    Args:
        audio_np (numpy.ndarray): The audio data to be transcribed.

    Returns:
        str: The transcribed text.
    """
    result = stt.transcribe(audio_np, fp16=False)  # Set fp16=True if using a GPU
    text = result["text"].strip()
    return text


def get_llm_response(text: str) -> str:
    """
    Generates a response to the given text using the Llama-2 language model.

    Args:
        text (str): The input text to be processed.

    Returns:
        str: The generated response.
    """
    response = chain.predict(input=text)
    if response.startswith("Assistant:"):
        response = response[len("Assistant:") :].strip()
    return response


def play_audio(sample_rate, audio_array):
    """
    Plays the given audio data using the sounddevice library.

    Args:
        sample_rate (int): The sample rate of the audio data.
        audio_array (numpy.ndarray): The audio data to be played.

    Returns:
        None
    """
    sd.play(audio_array, sample_rate)
    sd.wait()


if __name__ == "__main__":
    console.print("[cyan]Assistant started! Press Ctrl+C to exit.")

    try:
        while True:
            console.input(
                "Press Enter to start recording, then press Enter again to stop."
            )

            data_queue = Queue()  # Queue to store recorded audio data
            stop_event = threading.Event()  # Event to signal stop recording
            recording_thread = threading.Thread(
                target=record_audio,
                args=(stop_event, data_queue),
            )
            recording_thread.start()

            input()  # Wait for user to press Enter to stop recording
            stop_event.set()  # Signal stop recording
            recording_thread.join()  # Wait for recording thread to finish

            audio_data = b"".join(list(data_queue.queue))  # Combine recorded audio data
            audio_np = (
                np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
            )  # Convert audio data to numpy array

            if audio_np.size > 0:
                with console.status("Transcribing...", spinner="earth"):
                    text = transcribe(audio_np)
                console.print(f"[yellow]You: {text}")

                with console.status("Generating response...", spinner="earth"):
                    response = get_llm_response(text)
                    sample_rate, audio_array = tts.long_form_synthesize(response)

                console.print(f"[cyan]Assistant: {response}")
                play_audio(sample_rate, audio_array)
            else:
                console.print(
                    "[red]No audio recorded. Please ensure your microphone is working."
                )

    except KeyboardInterrupt:
        console.print("\n[red]Exiting...")

    console.print("[blue]Session ended.")
