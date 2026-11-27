import hmac
import os
from urllib.parse import quote

from shortGPT.audio.edge_voice_module import EdgeTTSVoiceModule
from shortGPT.config.api_db import ApiKeyManager
from shortGPT.config.languages import EDGE_TTS_VOICENAME_MAPPING, Language
from shortGPT.engine.content_video_engine import ContentVideoEngine
from shortGPT.gpt import gpt_chat_video


def render_video(topic: str, language: str, token: str) -> str:
    """Render a vertical ShortGPT video and return its downloadable URL."""
    expected_token = os.environ.get("SHORTGPT_API_TOKEN", "")
    if not expected_token or not hmac.compare_digest(str(token), expected_token):
        raise ValueError("Invalid worker token.")

    topic = (topic or "").strip()
    if not topic or len(topic) > 600:
        raise ValueError("Enter a topic between 1 and 600 characters.")

    if not (ApiKeyManager.get_api_key("OPENAI_API_KEY") or ApiKeyManager.get_api_key("GEMINI_API_KEY")):
        raise RuntimeError("Configure OPENAI_API_KEY or GEMINI_API_KEY in Railway.")
    if not ApiKeyManager.get_api_key("PEXELS_API_KEY"):
        raise RuntimeError("Configure PEXELS_API_KEY in Railway.")

    selected_language = Language(language)
    voice_module = EdgeTTSVoiceModule(EDGE_TTS_VOICENAME_MAPPING[selected_language]["male"])
    script = gpt_chat_video.generateScript(topic, selected_language.value)
    if not script:
        raise RuntimeError("ShortGPT could not generate a script.")

    engine = ContentVideoEngine(
        voiceModule=voice_module,
        script=script,
        isVerticalFormat=True,
        language=selected_language,
    )
    for _step, _description in engine.makeContent():
        pass

    video_path = engine.get_video_output_path()
    if not video_path or not os.path.isfile(video_path):
        raise RuntimeError("ShortGPT did not produce an MP4 file.")

    public_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if not public_domain:
        raise RuntimeError("Railway public domain is not configured.")
    return "https://" + public_domain + "/gradio_api/file=" + quote(video_path, safe="/")
