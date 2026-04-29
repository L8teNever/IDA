"""
Vision Worker - analysiert Bilder über Ollamas Vision-Modell (moondream).

Modell: moondream (~1.7GB RAM, CPU-freundlich)
Alternativ: llava-phi3 (~2.9GB, höhere Qualität)
"""

import logging
from agents.base import BaseAgent, AgentMessage, AgentResponse
import config

logger = logging.getLogger(__name__)

VISION_MODEL = "moondream"

SYSTEM_PROMPT = """Du bist ein Bildanalyst. Analysiere Bilder und beschreibe ihren Inhalt detailliert auf Deutsch.
Beantworte die Frage des Nutzers zum Bild präzise und hilfreich."""


class VisionWorker(BaseAgent):
    name = "vision_worker"
    description = "Analysiert Bilder und beantwortet Fragen zu Bildinhalten"

    def __init__(self):
        super().__init__()
        self.model = VISION_MODEL

    async def process(self, message: AgentMessage) -> AgentResponse:
        image_b64 = message.metadata.get("image_b64")

        if not image_b64:
            return AgentResponse(content="Kein Bild zum Analysieren vorhanden.", success=False)

        try:
            response = await self.client.chat(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": message.content,
                        "images": [image_b64],
                    }
                ],
            )
            return AgentResponse(content=response['message']['content'])
        except Exception as e:
            logger.error(f"Vision Worker Fehler: {e}")
            return AgentResponse(
                content=f"Bildanalyse fehlgeschlagen: {e}",
                success=False,
            )
