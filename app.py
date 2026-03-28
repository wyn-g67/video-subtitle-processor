# app.py
from fastapi import FastAPI, File, UploadFile, Request, Depends
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import uuid
import os
import json
from celery.result import AsyncResult
from tasks import preview_task, render_task, app as celery_app

from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.responses import JSONResponse

from database import init_db, get_db, Task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from contextlib import asynccontextmanager

# 初始化限流器
limiter = Limiter(key_func=get_remote_address)
#初始化
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("启动中...")
    init_db()
    yield
    print("关闭中...")

app = FastAPI(lifespan=lifespan)

# 在 FastAPI 应用初始化后添加
app.state.limiter = limiter
async def rate_limit_handler(request, exc):
    return JSONResponse(
        status_code=429,
        content={"error": "操作太频繁，请稍后再试（每分钟最多5次）"}
    )
app.add_exception_handler(RateLimitExceeded, rate_limit_handler)

# 允许跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 创建目录
os.makedirs("/tmp/video_uploads", exist_ok=True)
os.makedirs("/tmp/subtitle_data", exist_ok=True)
os.makedirs("/tmp/video_outputs", exist_ok=True)

# ========== 网页前端 ==========
@app.get("/", response_class=HTMLResponse)
async def get_index():
    """返回主页面"""
    with open("templates/index.html", "r", encoding="utf-8") as f:
        return f.read()

# ========== 上传接口 ==========
@app.post("/upload")
@limiter.limit("5/minute")  # 每分钟最多5次上传
async def upload_video(request: Request,file: UploadFile = File(...),db: AsyncSession = Depends(get_db)):
    """上传视频并启动预览任务"""
    try:
        
        # 1. 检查文件类型
        allowed_types = ['.mp4', '.avi', '.mov', '.mkv', '.flv']
        file_ext = os.path.splitext(file.filename)[1].lower()
        if file_ext not in allowed_types:
            return JSONResponse(
                status_code=400,
                content={"error": f"不支持的文件类型 {file_ext}，请上传 {', '.join(allowed_types)}"}
            )

        # 2. 读取文件内容并检查大小（限制50MB）
        content = await file.read()
        max_size = 50 * 1024 * 1024  # 50MB
        if len(content) > max_size:
            return JSONResponse(
                status_code=400,
                content={"error": f"文件太大（{len(content)/1024/1024:.1f}MB），不能超过50MB"}
            )

        # 3. 生成唯一ID并保存
        video_id = str(uuid.uuid4())
        print(f"app.py 生成的 video_id: {video_id}")
        video_path = f"/tmp/video_uploads/{video_id}.mp4"
        os.makedirs("/tmp/video_uploads", exist_ok=True)

        with open(video_path, "wb") as f:
            f.write(content)

        print(f"视频已保存: {video_path} (大小: {len(content)/1024/1024:.1f}MB)")

        # 创建任务记录
        task = Task(
            id=video_id,
            video_path=video_path,
            status="pending"
        )
        db.add(task)
        await db.commit()

        # 4. 启动预览任务
        task = preview_task.delay(video_path, video_id)

        return {
            "preview_task_id": task.id,
            "video_id": video_id,
            "file_size": f"{len(content)/1024/1024:.1f}MB"
        }

    except Exception as e:
        print(f"❌ 上传失败: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )
# ========== 获取字幕数据 ==========
@app.get("/subtitles/{video_id}")
async def get_subtitles(video_id: str, db: AsyncSession = Depends(get_db)):
    """从数据库获取字幕数据"""
    result = await db.execute(select(Task).where(Task.id == video_id))
    task = result.scalar_one_or_none()
    
    if not task:
        return {"error": "字幕数据不存在"}
    
    if task.subtitles is None:
        return {"error": "字幕数据尚未生成"}
    
    return {
        "video_id": task.id,
        "video_info": task.video_info,
        "subtitles": task.subtitles,
        "style": task.style or {}
    }
# ========== 渲染最终视频 ==========
@app.post("/render/{video_id}")
async def render_video(video_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        body = await request.json()
        subtitles = body.get('subtitles', [])
        style_params = body.get('style', {})
        
        # 更新数据库中的字幕和样式
        result = await db.execute(select(Task).where(Task.id == video_id))
        task = result.scalar_one_or_none()
        
        if task:
            task.subtitles = subtitles
            task.style = style_params
            task.status = "processing"
            await db.commit()
        
        video_path = f"/tmp/video_uploads/{video_id}.mp4"
        
        # 启动合成任务
        celery_task = render_task.delay(video_path, video_id, subtitles, style_params)
        
        return {"render_task_id": celery_task.id}
        
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# ========== 任务状态查询 ==========
@app.get("/task/{task_id}")
async def get_task_status(task_id: str, db: AsyncSession = Depends(get_db)):
    """查询任务状态（同时支持 Celery 任务ID 和 video_id）"""
    # 先按 Celery 任务ID查
    celery_result = AsyncResult(task_id, app=celery_app)
    
    if celery_result.state != 'PENDING':
        # 有 Celery 状态，直接返回
        if celery_result.state == 'SUCCESS':
            # 同时更新数据库
            result = await db.execute(select(Task).where(Task.id == celery_result.result.get('video_id')))
            task = result.scalar_one_or_none()
            if task:
                task.status = "completed"
                task.progress = 100
                task.output_path = celery_result.result.get('video_path')
                task.subtitle_path = celery_result.result.get('subtitle_path')
                await db.commit()
            return {'state': 'SUCCESS', 'progress': 100, 'result': celery_result.result}
        elif celery_result.state == 'PROGRESS':
            return {
                'state': 'PROGRESS',
                'progress': celery_result.info.get('progress', 0),
                'status': celery_result.info.get('status', '处理中...')
            }
        elif celery_result.state == 'FAILURE':
            return {'state': 'FAILURE', 'error': str(celery_result.info)}
    
    # 按 video_id 查数据库
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    
    if task:
        if task.status == "completed":
            return {
                'state': 'SUCCESS',
                'progress': 100,
                'result': {
                    'video_path': task.output_path,
                    'subtitle_path': task.subtitle_path,
                    'segments_count': len(task.subtitles) if task.subtitles else 0
                }
            }
        elif task.status == "failed":
            return {'state': 'FAILURE', 'error': task.error_message}
        else:
            return {'state': 'PROGRESS', 'progress': task.progress, 'status': task.status}
    
    return {'state': 'PENDING', 'progress': 0}
# ========== 获取原始视频 ==========
@app.get("/video/{video_id}")
async def get_video(video_id: str):
    """获取原始视频（用于预览）"""
    video_path = f"/tmp/video_uploads/{video_id}.mp4"
    if os.path.exists(video_path):
        return FileResponse(video_path)
    return {"error": "视频不存在"}

# ========== 文件下载 ==========
@app.get("/download/{filename}")
async def download_file(filename: str):
    """下载文件"""
    # 尝试在输出目录找文件
    file_path = os.path.join("/tmp/video_outputs", filename)
    
    if os.path.exists(file_path):
        return FileResponse(file_path)
    
    return {"error": "文件不存在"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
