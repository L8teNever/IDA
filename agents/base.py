from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import ollama
import config
from agents.worker_memory import WorkerMemory


@dataclass
class AgentMessage:
    content: str
    metadata: dict = field(default_factory=dict)


@dataclass
class AgentResponse:
    content: str
    success: bool = True
    metadata: dict = field(default_factory=dict)


class BaseAgent(ABC):
    name: str = "base"
    description: str = "Basis-Agent"
    model: str = None

    def __init__(self):
        self.client = ollama.AsyncClient(host=config.OLLAMA_URL)
        if self.model is None:
            self.model = config.WORKER_MODEL
        self.memory = WorkerMemory(self.name)

    @abstractmethod
    async def process(self, message: AgentMessage) -> AgentResponse:
        pass

    async def _chat(self, messages: list, system: str = None, num_predict: int = 400) -> str:
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)
        response = await self.client.chat(
            model=self.model,
            messages=full_messages,
            options={"num_predict": num_predict},
        )
        if isinstance(response, dict):
            return response["message"]["content"]
        return response.message.content
