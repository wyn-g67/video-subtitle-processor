# processor.py
import os
import whisper
from moviepy import VideoFileClip, ImageClip, CompositeVideoClip
from PIL import Image, ImageDraw, ImageFont
import numpy as np
import re
import requests
import tempfile
import json
import uuid
import asyncio
from database import AsyncSessionLocal, Task
from sqlalchemy import select
from database import SyncSessionLocal, Task
from config import GEMINI_API_KEY, WHISPER_API_KEY
# ================= 配置区 =================
WHISPER_BASE_URL = "https://api.siliconflow.cn/v1"
def update_task_status_sync(video_id, **kwargs):
    """同步更新任务状态（在 Celery Worker 里调用）"""
    with SyncSessionLocal() as db:
        task = db.query(Task).filter(Task.id == video_id).first()
        if task:
            for key, value in kwargs.items():
                if hasattr(task, key):
                    setattr(task, key, value)
        else:
            task = Task(id=video_id, **kwargs)
            db.add(task)
        db.commit()
        print(f"✅ 数据库已更新: {video_id}")

def transcribe_with_whisper_api(video_path, api_key):
    """使用Whisper API进行语音识别"""
    try:
        print("   步骤1: 从视频提取音频...")
        video = VideoFileClip(video_path)

        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp_audio:
            audio_path = tmp_audio.name
            print(f"   临时音频文件: {audio_path}")
            video.audio.write_audiofile(audio_path, fps=16000, logger=None)
        video.close()

        print("   步骤2: 调用LemonFox.ai API...")
        url = "https://api.lemonfox.ai/v1/audio/transcriptions"

        headers = {"Authorization": f"Bearer {api_key}"}

        with open(audio_path, 'rb') as f:
            files = {'file': ('audio.mp3', f, 'audio/mpeg')}
            data = {
                'language': 'english',
                'response_format': 'verbose_json'
            }
            response = requests.post(url, headers=headers, files=files, data=data)

        os.unlink(audio_path)

        if response.status_code == 200:
            result_json = response.json()
            print(f"   API调用成功")

            segments = []
            if 'segments' in result_json:
                for segment in result_json['segments']:
                    segments.append({
                        'start': segment.get('start', 0),
                        'end': segment.get('end', 0),
                        'text': segment.get('text', '').strip(),
                        'words': segment.get('words', [])
                    })
            else:
                text = result_json.get('text', '')
                if text:
                    sentences = re.split(r'(?<=[.!?])\s+', text)
                    current_time = 0
                    for sentence in sentences:
                        if sentence.strip():
                            duration = len(sentence.split()) * 0.4
                            segments.append({
                                'start': current_time,
                                'end': current_time + duration,
                                'text': sentence.strip(),
                                'words': []
                            })
                            current_time += duration

            print(f"   解析到 {len(segments)} 个片段")
            return {
                'segments': segments,
                'text': result_json.get('text', '')
            }
        else:
            print(f"   API错误: {response.status_code}")
            raise Exception(f"API返回错误: {response.status_code}")

    except Exception as e:
        print(f"   Whisper API调用失败: {e}")
        print("   步骤3: 回退到本地模型...")
        model = whisper.load_model("base")
        return model.transcribe(
            video_path,
            word_timestamps=True,
            condition_on_previous_text=False,
            verbose=True
        )

def get_chinese_font(font_size=36):
    """获取中文字体"""
    font_paths = [
        # Linux 中文字体路径
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        # Windows 字体路径（用于本地测试）
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        # 最后备选英文字体
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]

    for font_path in font_paths:
        if os.path.exists(font_path):
            try:
                font = ImageFont.truetype(font_path, font_size)
                print(f"使用字体: {os.path.basename(font_path)}")
                return font
            except Exception as e:
                print(f"字体加载失败 {font_path}: {e}")
                continue

    print("⚠️ 警告: 未找到任何字体，使用默认字体")
    return ImageFont.load_default()

def create_subtitle_image_with_style(text, video_width, style):
    """根据样式参数生成字幕图片"""
    try:
        # 从样式参数中获取值
        font_family = style.get('font_family', 'SimHei')
        font_size = style.get('font_size', 34)
        font_color = style.get('font_color', '#FFFF00')
        bg_color = style.get('bg_color', '#000000')
        bg_opacity = style.get('bg_opacity', 80)
        stroke_width = style.get('stroke_width', 2)
        
        # 字体映射
        font_map = {
            'SimHei': '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
            'Microsoft YaHei': '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
            'KaiTi': '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
            'SongTi': '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
        }
        
        # 获取字体路径
        font_path = font_map.get(font_family, font_map['SimHei'])
        
        # 加载字体
        try:
            font = ImageFont.truetype(font_path, font_size)
        except:
            print(f"字体加载失败: {font_path}，使用默认字体")
            font = ImageFont.load_default()
        
        # 计算文字大小
        temp_img = Image.new('RGB', (1, 1))
        temp_draw = ImageDraw.Draw(temp_img)
        bbox = temp_draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        
        # 自动换行
        max_width = video_width * 0.9
        if text_width > max_width:
            # 估算每行字符数
            avg_char_width = text_width / len(text) if text else 1
            chars_per_line = int(max_width / avg_char_width)
            if chars_per_line < 1:
                chars_per_line = 1
            
            lines = []
            for i in range(0, len(text), chars_per_line):
                lines.append(text[i:i+chars_per_line])
            
            # 重新计算多行大小
            line_heights = []
            max_line_width = 0
            for line in lines:
                bbox = temp_draw.textbbox((0, 0), line, font=font)
                line_width = bbox[2] - bbox[0]
                line_height = bbox[3] - bbox[1]
                line_heights.append(line_height)
                if line_width > max_line_width:
                    max_line_width = line_width
            
            text_width = min(max_line_width, max_width)
            text_height = sum(line_heights) + (len(lines) - 1) * 5
            multiline_text = lines
        else:
            multiline_text = [text]
        
        # 创建字幕图片
        padding = 12
        img_width = min(int(max_width), text_width + 2 * padding)
        img_height = text_height + 2 * padding
        
        # 转换颜色
        try:
            bg_rgb = tuple(int(bg_color.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
        except:
            bg_rgb = (0, 0, 0)
        
        bg_alpha = int(bg_opacity * 2.55)  # 0-100 -> 0-255
        
        try:
            font_rgb = tuple(int(font_color.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
        except:
            font_rgb = (255, 255, 0)
        
        # 创建图像
        img = Image.new('RGBA', (img_width, img_height), (*bg_rgb, bg_alpha))
        draw = ImageDraw.Draw(img)
        
        # 圆角矩形背景
        radius = 10
        draw.rounded_rectangle([0, 0, img_width, img_height], 
                              radius=radius, 
                              fill=(*bg_rgb, bg_alpha))
        
        # 绘制文字
        y = padding
        for line in multiline_text:
            bbox = draw.textbbox((0, 0), line, font=font)
            line_width = bbox[2] - bbox[0]
            x = (img_width - line_width) // 2
            
            # 文字描边
            for dx in [-stroke_width, stroke_width, 0, 0]:
                for dy in [0, 0, -stroke_width, stroke_width]:
                    if dx != 0 or dy != 0:
                        draw.text((x+dx, y+dy), line, font=font, fill=(0, 0, 0, 255))
            
            # 主文字
            draw.text((x, y), line, font=font, fill=(*font_rgb, 255))
            y += (bbox[3] - bbox[1]) + 5
        
        return np.array(img), img_height
        
    except Exception as e:
        print(f"样式化字幕生成失败: {e}")
        # 返回空白字幕
        img = Image.new('RGBA', (video_width, 60), (0, 0, 0, 180))
        return np.array(img), 60

def translate_with_gemini(texts, api_key):
    """使用一步API中转调用Gemini翻译（OpenAI格式）"""
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url="https://yibuapi.com/v1",
            timeout=30
        )

        prompt = """你是一个逐句翻译器。你的工作是将输入文本逐行翻译，输出必须与输入行数完全一致。

示例：
输入：
Hello world.
How are you?
I am fine.

输出：
你好，世界。
你好吗？
我很好。

现在请翻译以下内容：
""" + "\n".join([f"{i+1}. {text}" for i, text in enumerate(texts)])

        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "user", "content": prompt}
            ],
            temperature=0
        )

        result_text = response.choices[0].message.content.strip()
        lines = [line.strip() for line in result_text.split('\n') if line.strip()]

        translations = []
        for line in lines:
            clean_line = re.sub(r'^\d+[\.、]?\s*', '', line)
            translations.append(clean_line)

        if len(translations) != len(texts):
            translations = result_text.split('\n')[:len(texts)]

        return translations

    except Exception as e:
        print(f"翻译错误: {e}")
        import traceback
        traceback.print_exc()
        return texts

def process_video_for_preview(video_path, video_id):
    """
    只生成字幕数据，不合成视频（用于前端预览）
    """
    try:
        # 获取视频信息
        video = VideoFileClip(video_path)
        video_info = {
            'width': video.w,
            'height': video.h,
            'duration': video.duration,
            'fps': video.fps
        }
        video.close()
        print(f"process_video_for_preview 收到的 video_id: {video_id}")
        # 1. 语音识别
        result = transcribe_with_whisper_api(video_path, WHISPER_API_KEY)
        segments = result['segments']
        
        # 2. 翻译
        texts = [seg['text'] for seg in segments]
        translations = translate_with_gemini(texts, GEMINI_API_KEY)
        
        # 3. 构建字幕数据
        subtitles = []
        for i, seg in enumerate(segments):
            if i < len(translations):
                subtitles.append({
                    'id': i,
                    'start': seg['start'],
                    'end': seg['end'],
                    'original': seg['text'],
                    'translated': translations[i],
                    'words': seg.get('words', [])
                })
        
        # 5. 保存数据
        update_task_status_sync(
            video_id,
            video_path=video_path,
            video_info=video_info,
            subtitles=subtitles,
            style={
                'font_family': 'SimHei',
                'font_size': 34,
                'font_color': '#FFFF00',
                'bg_color': '#000000',
                'bg_opacity': 80,
                'position': 'bottom',
                'stroke_width': 2
            },
            status="preview_ready",
            progress=100
        )

        # 5. 返回结果
        return {
            'status': 'success',
            'video_id': video_id,
            'video_info': video_info,
            'subtitles': subtitles,
            'segments_count': len(subtitles)
        }
    except Exception as e:
        return {'status': 'failed', 'error': str(e)}

def render_video_with_subtitles(video_path, output_path, subtitles, style_params=None):
    """
    使用已有的字幕数据合成视频（不重新识别翻译）
    """
    try:
        print(f"开始使用已有字幕合成视频: {video_path}")
        
        # 加载视频
        video = VideoFileClip(video_path)
        
        # 默认样式
        default_style = {
            'font_family': 'SimHei',
            'font_size': 34,
            'font_color': '#FFFF00',
            'bg_color': '#000000',
            'bg_opacity': 80,
            'stroke_width': 2,
            'position': 'bottom',
            'margin_bottom': 60
        }
        
        # 合并样式参数
        if style_params:
            current_style = {**default_style, **style_params}
        else:
            current_style = default_style
        
        # 创建字幕剪辑
        subtitle_clips = []
        for sub in subtitles:
            start_time = sub['start']
            end_time = sub['end']
            duration = max(0.5, end_time - start_time)
            text = sub.get('translated', sub.get('original', ''))
            
            try:
                subtitle_img, img_height = create_subtitle_image_with_style(
                    text, 
                    video.w, 
                    current_style
                )
                txt_clip = ImageClip(subtitle_img, duration=duration)
                
                # 根据位置调整Y坐标
                if current_style.get('position') == 'top':
                    y_pos = 60
                elif current_style.get('position') == 'middle':
                    y_pos = (video.h - img_height) // 2
                else:
                    y_pos = video.h - img_height - current_style.get('margin_bottom', 60)
                
                txt_clip = txt_clip.with_position(('center', y_pos))
                txt_clip = txt_clip.with_start(start_time)
                subtitle_clips.append(txt_clip)
            except Exception as e:
                print(f"字幕生成失败: {e}")
                continue
        
        # 合成视频
        if subtitle_clips:
            subtitle_clips.sort(key=lambda x: x.start)
            final = CompositeVideoClip([video] + subtitle_clips)
        else:
            final = video
        
        # 输出视频
        final.write_videofile(
            output_path,
            fps=video.fps,
            codec='libx264',
            audio_codec='aac',
            logger=None
        )
        
        video.close()
        final.close()
        
        # 生成SRT字幕文件
        subtitle_path = output_path.replace('.mp4', '.srt')
        with open(subtitle_path, 'w', encoding='utf-8') as f:
            for i, sub in enumerate(subtitles):
                start = sub['start']
                end = sub['end']
                text = sub.get('translated', sub.get('original', ''))
                start_str = f"{int(start//3600):02d}:{int((start%3600)//60):02d}:{int(start%60):02d},{int((start%1)*1000):03d}"
                end_str = f"{int(end//3600):02d}:{int((end%3600)//60):02d}:{int(end%60):02d},{int((end%1)*1000):03d}"
                f.write(f"{i+1}\n{start_str} --> {end_str}\n{text}\n\n")
        
        return {
            'status': 'success',
            'video_path': output_path,
            'subtitle_path': subtitle_path,
            'segments_count': len(subtitles)
        }
        
    except Exception as e:
        return {
            'status': 'failed',
            'error': str(e)
        }
