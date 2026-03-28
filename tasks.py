# tasks.py
from celery import Celery
from processor import process_video_for_preview, render_video_with_subtitles
import os

app = Celery(
    'tasks',
    broker='redis://localhost:6379/0',
    backend='redis://localhost:6379/0'  # 必须指定 backend
)

# 配置
app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='Asia/Shanghai',
    enable_utc=False,
    task_track_started=True,
    task_time_limit=3600,
    task_soft_time_limit=3000,
)

@app.task(bind=True, name='preview_task')
def preview_task(self, video_path, video_id):
    """
    预览任务：生成字幕数据
    """
    try:
        print(f"预览任务开始: {video_path}")
        self.update_state(state='PROGRESS', meta={'progress': 10, 'status': '开始语音识别...'})
        
        result = process_video_for_preview(video_path,video_id)
        
        if result['status'] == 'success':
            self.update_state(state='PROGRESS', meta={'progress': 100, 'status': '预览数据生成完成'})
            return result
        else:
            raise Exception(result.get('error', '未知错误'))
            
    except Exception as e:
        print(f"预览任务失败: {e}")
        self.update_state(state='FAILURE', meta={'error': str(e)})
        raise

@app.task(bind=True, name='render_task')
def render_task(self, video_path, video_id, subtitles, style_params=None):
    """
    合成任务：使用前端传回的字幕数据直接合成视频
    不再重新识别翻译！
    """
    try:
        print(f"合成任务开始: {video_path}, video_id={video_id}")
        self.update_state(state='PROGRESS', meta={'progress': 10, 'status': '开始合成视频...'})
        
        output_path = f"/tmp/video_outputs/{video_id}_final.mp4"
        os.makedirs("/tmp/video_outputs", exist_ok=True)
        
        # 直接使用前端传回的字幕数据
        result = render_video_with_subtitles(video_path, output_path, subtitles, style_params)
        
        if result['status'] == 'success':
            self.update_state(state='PROGRESS', meta={'progress': 100, 'status': '合成完成'})
            print(f"合成任务完成: {output_path}")
            return result
        else:
            raise Exception(result.get('error', '未知错误'))
            
    except Exception as e:
        print(f"合成任务失败: {e}")
        self.update_state(state='FAILURE', meta={'error': str(e)})
        raise
