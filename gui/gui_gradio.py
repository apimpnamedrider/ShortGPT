import gradio as gr
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
import uvicorn

from gui.content_automation_ui import GradioContentAutomationUI
from gui.ui_abstract_base import AbstractBaseUI
from gui.ui_components_html import GradioComponentsHTML
from gui.ui_tab_asset_library import AssetLibrary
from gui.ui_tab_config import ConfigUI
from shortGPT.utils.cli import CLI
from shortGPT.api_server import render_video, start_render_job, get_render_job


class ShortGptUI(AbstractBaseUI):
    '''Class for the GUI. This class is responsible for creating the UI and launching the server.'''

    def __init__(self, colab=False):
        super().__init__(ui_name='gradio_shortgpt')
        self.colab = colab
        CLI.display_header()

    def create_interface(self):
        '''Create Gradio interface'''
        with gr.Blocks(theme=gr.themes.Default(spacing_size=gr.themes.sizes.spacing_sm), css="footer {visibility: hidden}", title="ShortGPT Demo") as shortGptUI:
            with gr.Row(variant='compact'):
                gr.HTML(GradioComponentsHTML.get_html_header())

            self.content_automation = GradioContentAutomationUI(shortGptUI).create_ui()
            self.asset_library_ui = AssetLibrary().create_ui()
            self.config_ui = ConfigUI().create_ui()

            # Hidden components expose a token-protected API endpoint for Shorts Forge.
            api_topic = gr.Textbox(visible=False)
            api_language = gr.Textbox(value="English", visible=False)
            api_token = gr.Textbox(visible=False)
            api_storyboard = gr.Textbox(visible=False)
            api_result = gr.Textbox(visible=False)
            api_trigger = gr.Button(visible=False)
            api_trigger.click(
                render_video,
                inputs=[api_topic, api_language, api_token, api_storyboard],
                outputs=[api_result],
                api_name="render_video",
                show_progress="hidden",
            )
        return shortGptUI

    def launch(self):
        '''Launch Gradio plus a lightweight asynchronous render-job API.'''
        shortGptUI = self.create_interface()
        app = FastAPI()

        @app.post("/api/render-jobs")
        async def create_render_job(request: Request):
            payload = await request.json()
            job_id = start_render_job(
                str(payload.get("topic") or ""),
                str(payload.get("language") or "English"),
                str(payload.get("token") or ""),
                str(payload.get("storyboard") or "")
            )
            return JSONResponse({"jobId": job_id, "status": "queued"})

        @app.get("/api/render-jobs/{job_id}")
        async def render_job_status(job_id: str):
            job = get_render_job(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="Render job not found or expired.")
            result = {key: value for key, value in job.items() if key != "createdAt"}
            return JSONResponse(result)

        app = gr.mount_gradio_app(
            app, shortGptUI.queue(),
            path="/",
            allowed_paths=["public/", "videos/", "fonts/", "/app/.database/videos/"]
        )
        print("\nShortGPT and Shorts Forge job API running on http://0.0.0.0:31415\n")
        uvicorn.run(app, host="0.0.0.0", port=31415)



if __name__ == "__main__":
    app = ShortGptUI()
    app.launch()


import signal

def signal_handler(sig, frame):
    print("Closing Gradio server...")
    import gradio as gr
    gr.close_all()
    exit(0)

signal.signal(signal.SIGINT, signal_handler)