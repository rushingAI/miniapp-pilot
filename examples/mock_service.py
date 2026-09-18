"""In-memory synthetic task fixtures. No credentials, telemetry or persistence."""
from threading import Lock
from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI()
_tasks = []
_lock = Lock()

class Task(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    completed: bool = False

class Seed(BaseModel):
    tasks: list[Task] = Field(max_length=100)

@app.get('/health')
def health():
    return {'ready': True}

@app.post('/seed')
def seed(payload: Seed):
    global _tasks
    with _lock:
        _tasks = [task.model_dump() for task in payload.tasks]
    return {'ready': True, 'count': len(payload.tasks)}

@app.get('/tasks')
def tasks():
    with _lock:
        return {'tasks': list(_tasks)}

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8766)
