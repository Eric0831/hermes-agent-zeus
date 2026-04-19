"""
Advanced Image Processing Tool for HERMES
整合了背景移除、Instagram 濾鏡、素描效果、OpenCV 濾鏡等功能
"""
import json
import os
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageDraw
import numpy as np

# 檢查依賴
try:
    from rembg import remove
    HAS_REMBG = True
except ImportError:
    HAS_REMBG = False

try:
    import cv2
    HAS_OPENCV = True
except ImportError:
    HAS_OPENCV = False

from tools.registry import registry

def check_requirements() -> bool:
    """檢查依賴是否安裝"""
    return True  # Pillow 已安裝

# ============================================================================
# 1. 背景移除
# ============================================================================

def remove_background(input_path: str, output_path: str = None) -> str:
    """
    移除圖片背景
    
    Args:
        input_path: 輸入圖片路徑
        output_path: 輸出圖片路徑（可選，預設自動生成）
    
    Returns:
        輸出圖片路徑
    """
    if not HAS_REMBG:
        return json.dumps({"error": "rembg not installed. Please install with: pip install rembg"})
    
    if not output_path:
        base = os.path.splitext(input_path)[0]
        output_path = f"{base}_nobg.png"
    
    try:
        input_image = Image.open(input_path)
        
        # 如果有 alpha 通道，轉換為 RGB
        if input_image.mode in ('RGBA', 'LA'):
            input_image = input_image.convert('RGB')
        
        # 移除背景
        output_image = remove(input_image)
        output_image.save(output_path)
        
        file_size = os.path.getsize(output_path) / 1024
        
        return json.dumps({
            "success": True,
            "message": "背景移除成功",
            "output_path": output_path,
            "file_size_kb": round(file_size, 2),
            "input_size": list(input_image.size),
            "output_size": list(output_image.size)
        }, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"error": f"背景移除失敗：{str(e)}"}, ensure_ascii=False)

# ============================================================================
# 2. Instagram 濾鏡集合
# ============================================================================

def apply_instagram_filter(input_path: str, filter_name: str = "clarendon", 
                          output_path: str = None) -> str:
    """
    應用 Instagram 風格濾鏡
    
    Args:
        input_path: 輸入圖片路徑
        filter_name: 濾鏡名稱 (clarendon, valencia, xpro2, lofi, hefe, inkwell, etc.)
        output_path: 輸出圖片路徑（可選）
    
    Returns:
        JSON 結果
    """
    if not output_path:
        base = os.path.splitext(input_path)[0]
        output_path = f"{base}_{filter_name}.jpg"
    
    try:
        img = Image.open(input_path)
        
        if img.mode in ('RGBA', 'LA'):
            img = img.convert('RGB')
        
        # 應用不同濾鏡
        if filter_name == "clarendon":
            # 增加對比度和暖色
            img = ImageEnhance.Contrast(img).enhance(1.1)
            img = ImageEnhance.Brightness(img).enhance(1.1)
            img = ImageEnhance.Color(img).enhance(1.25)
            
        elif filter_name == "valencia":
            # 溫暖、復古
            img = ImageEnhance.Contrast(img).enhance(0.9)
            img = ImageEnhance.Brightness(img).enhance(1.2)
            img = ImageEnhance.Color(img).enhance(0.85)
            # 添加暖色 overlay
            width, height = img.size
            overlay = Image.new('RGBA', (width, height), (255, 200, 150, 30))
            img = Image.blend(img, overlay.convert('RGB'), 0.15)
            
        elif filter_name == "xpro2":
            # 膠片效果
            img = ImageEnhance.Contrast(img).enhance(1.2)
            img = ImageEnhance.Color(img).enhance(1.1)
            img = ImageEnhance.Sharpness(img).enhance(1.1)
            # 添加輕微顆粒
            img = img.filter(ImageFilter.SMOOTH)
            
        elif filter_name == "lofi":
            # 低飽和度
            img = ImageEnhance.Color(img).enhance(0.7)
            img = ImageEnhance.Contrast(img).enhance(0.9)
            img = ImageEnhance.Brightness(img).enhance(1.1)
            
        elif filter_name == "hefe":
            # 明亮、清新
            img = ImageEnhance.Brightness(img).enhance(1.2)
            img = ImageEnhance.Color(img).enhance(1.1)
            img = ImageEnhance.Contrast(img).enhance(1.05)
            
        elif filter_name == "inkwell":
            # 黑白
            img = ImageOps.grayscale(img)
            img = ImageEnhance.Contrast(img).enhance(1.3)
            img = img.convert('RGB')
            
        elif filter_name == "1977":
            # 暖色復古
            img = ImageEnhance.Brightness(img).enhance(1.1)
            img = ImageEnhance.Contrast(img).enhance(1.1)
            img = ImageEnhance.Color(img).enhance(1.2)
            # 添加暖色調
            width, height = img.size
            overlay = Image.new('RGBA', (width, height), (255, 180, 120, 40))
            img = Image.blend(img, overlay.convert('RGB'), 0.2)
            
        elif filter_name == "faded":
            # 褪色效果
            img = ImageEnhance.Color(img).enhance(0.6)
            img = ImageEnhance.Contrast(img).enhance(0.8)
            img = ImageEnhance.Brightness(img).enhance(1.15)
            
        elif filter_name == "earlybird":
            # 早期鳥（暖色）
            img = ImageEnhance.Brightness(img).enhance(1.05)
            img = ImageEnhance.Contrast(img).enhance(1.1)
            img = ImageEnhance.Color(img).enhance(1.15)
            # 添加暖色調
            width, height = img.size
            overlay = Image.new('RGBA', (width, height), (255, 220, 180, 35))
            img = Image.blend(img, overlay.convert('RGB'), 0.18)
            
        elif filter_name == "reyes":
            # Reyes（柔和）
            img = ImageEnhance.Brightness(img).enhance(1.1)
            img = ImageEnhance.Color(img).enhance(1.05)
            img = img.filter(ImageFilter.SMOOTH)
            
        elif filter_name == "hudson":
            # Hudson（冷色）
            img = ImageEnhance.Contrast(img).enhance(1.15)
            img = ImageEnhance.Color(img).enhance(0.9)
            # 添加冷色調
            width, height = img.size
            overlay = Image.new('RGBA', (width, height), (180, 200, 255, 30))
            img = Image.blend(img, overlay.convert('RGB'), 0.15)
            
        else:
            return json.dumps({"error": f"未知的濾鏡：{filter_name}. 可用：clarendon, valencia, xpro2, lofi, hefe, inkwell, 1977, faded, earlybird, reyes, hudson"})
        
        img.save(output_path, 'JPEG', quality=95)
        
        file_size = os.path.getsize(output_path) / 1024
        
        return json.dumps({
            "success": True,
            "message": f"已應用 {filter_name} 濾鏡",
            "output_path": output_path,
            "file_size_kb": round(file_size, 2),
            "filter": filter_name
        }, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"error": f"濾鏡應用失敗：{str(e)}"}, ensure_ascii=False)

# ============================================================================
# 3. 素描效果
# ============================================================================

def image_to_sketch(input_path: str, output_path: str = None) -> str:
    """
    將圖片轉換為素描效果
    
    Args:
        input_path: 輸入圖片路徑
        output_path: 輸出圖片路徑（可選）
    
    Returns:
        JSON 結果
    """
    if not HAS_OPENCV:
        return json.dumps({"error": "OpenCV not installed. Please install with: pip install opencv-python-headless"})
    
    if not output_path:
        base = os.path.splitext(input_path)[0]
        output_path = f"{base}_sketch.jpg"
    
    try:
        # 使用 OpenCV 讀取圖片
        img = cv2.imread(input_path)
        if img is None:
            return json.dumps({"error": "無法讀取圖片"})
        
        # 轉換為灰度
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        
        # 反轉灰度
        gray_inv = 255 - gray
        
        # 高斯模糊
        blur = cv2.GaussianBlur(gray_inv, (21, 21), 0)
        
        # 顏色燒蝕（創建素描效果）
        sketch = cv2.divide(gray, blur, scale=256)
        
        # 保存
        cv2.imwrite(output_path, sketch)
        
        file_size = os.path.getsize(output_path) / 1024
        
        return json.dumps({
            "success": True,
            "message": "素描效果轉換成功",
            "output_path": output_path,
            "file_size_kb": round(file_size, 2)
        }, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"error": f"素描轉換失敗：{str(e)}"}, ensure_ascii=False)

# ============================================================================
# 4. OpenCV 濾鏡集合
# ============================================================================

def apply_opencv_filter(input_path: str, filter_name: str, 
                       output_path: str = None, **params) -> str:
    """
    應用 OpenCV 濾鏡
    
    Args:
        input_path: 輸入圖片路徑
        filter_name: 濾鏡名稱 (blur, sharpen, edge_detect, etc.)
        output_path: 輸出圖片路徑（可選）
        params: 額外參數
    
    Returns:
        JSON 結果
    """
    if not HAS_OPENCV:
        return json.dumps({"error": "OpenCV not installed"})
    
    if not output_path:
        base = os.path.splitext(input_path)[0]
        output_path = f"{base}_{filter_name}.jpg"
    
    try:
        img = cv2.imread(input_path)
        if img is None:
            return json.dumps({"error": "無法讀取圖片"})
        
        if filter_name == "blur":
            # 高斯模糊
            ksize = params.get('ksize', 5)
            result = cv2.GaussianBlur(img, (ksize, ksize), 0)
            
        elif filter_name == "sharpen":
            # 銳化
            kernel = params.get('kernel', np.array([[-1,-1,-1], 
                                                  [-1, 8,-1], 
                                                  [-1,-1,-1]]))
            result = cv2.filter2D(img, -1, kernel)
            
        elif filter_name == "edge_detect":
            # 邊緣偵測
            threshold1 = params.get('threshold1', 100)
            threshold2 = params.get('threshold2', 200)
            result = cv2.Canny(img, threshold1, threshold2)
            result = cv2.cvtColor(result, cv2.COLOR_GRAY2BGR)
            
        elif filter_name == "emboss":
            # 浮雕效果
            kernel = np.array([[ -2,-1, 0],
                              [ -1, 1, 1],
                              [  0, 1, 2]])
            result = cv2.filter2D(img, -1, kernel)
            
        elif filter_name == "solarize":
            # 陽光照相
            threshold = params.get('threshold', 128)
            result = cv2.adaptiveThreshold(
                cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 
                255, cv2.THRESH_BINARY, cv2.ADAPTIVE_THRESH_MEAN_C, 
                11, threshold
            )
            result = cv2.cvtColor(result, cv2.COLOR_GRAY2BGR)
            
        elif filter_name == "cartoon":
            # 卡通效果
            numFilters = params.get('numFilters', 7)
            sigmaColor = params.get('sigmaColor', 81)
            sigmaSpace = params.get('sigmaSpace', 81)
            img = cv2.bilateralFilter(img, 9, sigmaColor, sigmaSpace)
            for _ in range(numFilters):
                img = cv2.bilateralFilter(img, 9, sigmaColor, sigmaSpace)
            result = img
            
        elif filter_name == "sepia":
            # 懷舊棕褐色
            img_gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            img_sepia = np.zeros(img.shape, np.uint8)
            for r in range(3):
                for c in range(3):
                    for i in range(img.shape[0]):
                        for j in range(img.shape[1]):
                            if r == 0:
                                img_sepia[i, j, r] = min(255, img_gray[i, j] * 0.131)
                            elif r == 1:
                                img_sepia[i, j, r] = min(255, img_gray[i, j] * 0.274)
                            else:
                                img_sepia[i, j, r] = min(255, img_gray[i, j] * 0.528)
            result = img_sepia
            
        elif filter_name == "invert":
            # 反色
            result = cv2.bitwise_not(img)
            
        elif filter_name == "threshold":
            # 二值化
            thresh = params.get('thresh', 127)
            _, result = cv2.threshold(
                cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 
                thresh, 255, cv2.THRESH_BINARY
            )
            result = cv2.cvtColor(result, cv2.COLOR_GRAY2BGR)
            
        elif filter_name == "denoise":
            # 降噪
            h = params.get('h', 10)
            result = cv2.fastNlMeansDenoisingColored(img, None, h, h, 7, 21)
            
        elif filter_name == "oil_paint":
            # 油畫效果
            result = cv2.styleTransfer(img, None, None)
            
        else:
            return json.dumps({"error": f"未知的濾鏡：{filter_name}"})
        
        cv2.imwrite(output_path, result)
        
        file_size = os.path.getsize(output_path) / 1024
        
        return json.dumps({
            "success": True,
            "message": f"已應用 {filter_name} 濾鏡",
            "output_path": output_path,
            "file_size_kb": round(file_size, 2),
            "filter": filter_name
        }, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"error": f"濾鏡應用失敗：{str(e)}"}, ensure_ascii=False)

# ============================================================================
# 5. 圖片增強
# ============================================================================

def enhance_image(input_path: str, enhancements: dict = None, 
                 output_path: str = None) -> str:
    """
    增強圖片品質
    
    Args:
        input_path: 輸入圖片路徑
        enhancements: 增強參數 {brightness, contrast, saturation, sharpness}
        output_path: 輸出圖片路徑（可選）
    
    Returns:
        JSON 結果
    """
    if not output_path:
        base = os.path.splitext(input_path)[0]
        output_path = f"{base}_enhanced.jpg"
    
    if not enhancements:
        enhancements = {
            "brightness": 1.1,
            "contrast": 1.2,
            "saturation": 1.3,
            "sharpness": 1.2
        }
    
    try:
        img = Image.open(input_path)
        
        if img.mode in ('RGBA', 'LA'):
            img = img.convert('RGB')
        
        # 應用增強
        if enhancements.get('brightness'):
            img = ImageEnhance.Brightness(img).enhance(enhancements['brightness'])
        
        if enhancements.get('contrast'):
            img = ImageEnhance.Contrast(img).enhance(enhancements['contrast'])
        
        if enhancements.get('saturation'):
            img = ImageEnhance.Color(img).enhance(enhancements['saturation'])
        
        if enhancements.get('sharpness'):
            img = ImageEnhance.Sharpness(img).enhance(enhancements['sharpness'])
        
        img.save(output_path, 'JPEG', quality=95)
        
        file_size = os.path.getsize(output_path) / 1024
        
        return json.dumps({
            "success": True,
            "message": "圖片增強成功",
            "output_path": output_path,
            "file_size_kb": round(file_size, 2),
            "enhancements": enhancements
        }, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"error": f"圖片增強失敗：{str(e)}"}, ensure_ascii=False)

# ============================================================================
# 6. 日式和服風格（已整合）
# ============================================================================

def apply_kimono_style(input_path: str, output_path: str = None) -> str:
    """
    應用日式和服風格
    
    Args:
        input_path: 輸入圖片路徑
        output_path: 輸出圖片路徑（可選）
    
    Returns:
        JSON 結果
    """
    if not output_path:
        base = os.path.splitext(input_path)[0]
        output_path = f"{base}_kimono.jpg"
    
    try:
        img = Image.open(input_path)
        
        if img.mode in ('RGBA', 'LA'):
            img = img.convert('RGB')
        
        # 1. 強烈增強顏色
        img = ImageEnhance.Color(img).enhance(1.7)
        
        # 2. 增加對比度
        img = ImageEnhance.Contrast(img).enhance(1.3)
        
        # 3. 增加亮度
        img = ImageEnhance.Brightness(img).enhance(1.1)
        
        # 4. 增加銳度
        img = ImageEnhance.Sharpness(img).enhance(1.5)
        
        # 5. 柔焦效果
        img = img.filter(ImageFilter.SMOOTH)
        
        # 6. 添加日式暖色調
        width, height = img.size
        overlay = Image.new('RGBA', (width, height), (255, 140, 160, 30))
        img = Image.blend(img, overlay.convert('RGB'), 0.25)
        
        # 7. 添加日式風格邊框
        border_size = 35
        bordered = Image.new('RGB', (width + 2*border_size, height + 2*border_size), (255, 252, 238))
        bordered.paste(img, (border_size, border_size))
        
        # 8. 添加裝飾性邊框
        draw = ImageDraw.Draw(bordered)
        border_color = (178, 34, 34)
        for i in range(2):
            draw.rectangle(
                [border_size-7-i, border_size-7-i, 
                 width+border_size+6+i, height+border_size+6+i],
                outline=border_color,
                width=3
            )
        
        # 9. 添加文字標記
        try:
            font_paths = [
                "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
                "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
            ]
            font = None
            for fp in font_paths:
                if os.path.exists(fp):
                    try:
                        font = ImageFont.truetype(fp, 28)
                        break
                    except:
                        continue
            
            if font:
                text = "和服"
                bbox = draw.textbbox((0, 0), text, font=font)
                text_width = bbox[2] - bbox[0]
                text_height = bbox[3] - bbox[1]
                text_pos = (width + border_size - text_width - 25, height + border_size - text_height - 25)
                draw.text(text_pos, text, fill=(160, 40, 40), font=font)
        except:
            pass
        
        bordered.save(output_path, 'JPEG', quality=95)
        
        file_size = os.path.getsize(output_path) / 1024
        
        return json.dumps({
            "success": True,
            "message": "日式和服風格應用成功",
            "output_path": output_path,
            "file_size_kb": round(file_size, 2)
        }, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"error": f"風格應用失敗：{str(e)}"}, ensure_ascii=False)

# ============================================================================
# 註冊工具
# ============================================================================

registry.register(
    name="remove_background",
    toolset="image_processing",
    schema={
        "name": "remove_background",
        "description": "移除圖片背景。使用 AI 自動分割技術移除圖片背景。",
        "parameters": {
            "type": "object",
            "properties": {
                "input_path": {
                    "type": "string",
                    "description": "輸入圖片路徑"
                },
                "output_path": {
                    "type": "string",
                    "description": "輸出圖片路徑（可選）"
                }
            },
            "required": ["input_path"]
        }
    },
    handler=lambda args, **kw: remove_background(
        input_path=args.get("input_path", ""),
        output_path=args.get("output_path")
    ),
    check_fn=check_requirements,
)

registry.register(
    name="apply_instagram_filter",
    toolset="image_processing",
    schema={
        "name": "apply_instagram_filter",
        "description": "應用 Instagram 風格濾鏡。可用濾鏡：clarendon, valencia, xpro2, lofi, hefe, inkwell, 1977, faded, earlybird, reyes, hudson",
        "parameters": {
            "type": "object",
            "properties": {
                "input_path": {
                    "type": "string",
                    "description": "輸入圖片路徑"
                },
                "filter_name": {
                    "type": "string",
                    "description": "濾鏡名稱",
                    "enum": ["clarendon", "valencia", "xpro2", "lofi", "hefe", "inkwell", "1977", "faded", "earlybird", "reyes", "hudson"]
                },
                "output_path": {
                    "type": "string",
                    "description": "輸出圖片路徑（可選）"
                }
            },
            "required": ["input_path", "filter_name"]
        }
    },
    handler=lambda args, **kw: apply_instagram_filter(
        input_path=args.get("input_path", ""),
        filter_name=args.get("filter_name", "clarendon"),
        output_path=args.get("output_path")
    ),
    check_fn=check_requirements,
)

registry.register(
    name="image_to_sketch",
    toolset="image_processing",
    schema={
        "name": "image_to_sketch",
        "description": "將圖片轉換為素描效果。使用 OpenCV 創建藝術素描效果。",
        "parameters": {
            "type": "object",
            "properties": {
                "input_path": {
                    "type": "string",
                    "description": "輸入圖片路徑"
                },
                "output_path": {
                    "type": "string",
                    "description": "輸出圖片路徑（可選）"
                }
            },
            "required": ["input_path"]
        }
    },
    handler=lambda args, **kw: image_to_sketch(
        input_path=args.get("input_path", ""),
        output_path=args.get("output_path")
    ),
    check_fn=check_requirements,
)

registry.register(
    name="apply_opencv_filter",
    toolset="image_processing",
    schema={
        "name": "apply_opencv_filter",
        "description": "應用 OpenCV 濾鏡。可用濾鏡：blur, sharpen, edge_detect, emboss, solarize, cartoon, sepia, invert, threshold, denoise",
        "parameters": {
            "type": "object",
            "properties": {
                "input_path": {
                    "type": "string",
                    "description": "輸入圖片路徑"
                },
                "filter_name": {
                    "type": "string",
                    "description": "濾鏡名稱"
                },
                "output_path": {
                    "type": "string",
                    "description": "輸出圖片路徑（可選）"
                }
            },
            "required": ["input_path", "filter_name"]
        }
    },
    handler=lambda args, **kw: apply_opencv_filter(
        input_path=args.get("input_path", ""),
        filter_name=args.get("filter_name", ""),
        output_path=args.get("output_path")
    ),
    check_fn=check_requirements,
)

registry.register(
    name="enhance_image",
    toolset="image_processing",
    schema={
        "name": "enhance_image",
        "description": "增強圖片品質。可調整亮度、對比度、飽和度、銳度。",
        "parameters": {
            "type": "object",
            "properties": {
                "input_path": {
                    "type": "string",
                    "description": "輸入圖片路徑"
                },
                "enhancements": {
                    "type": "object",
                    "description": "增強參數 {brightness, contrast, saturation, sharpness}",
                    "properties": {
                        "brightness": {"type": "number", "description": "亮度係數"},
                        "contrast": {"type": "number", "description": "對比度係數"},
                        "saturation": {"type": "number", "description": "飽和度係數"},
                        "sharpness": {"type": "number", "description": "銳度係數"}
                    }
                },
                "output_path": {
                    "type": "string",
                    "description": "輸出圖片路徑（可選）"
                }
            },
            "required": ["input_path"]
        }
    },
    handler=lambda args, **kw: enhance_image(
        input_path=args.get("input_path", ""),
        enhancements=args.get("enhancements"),
        output_path=args.get("output_path")
    ),
    check_fn=check_requirements,
)

registry.register(
    name="apply_kimono_style",
    toolset="image_processing",
    schema={
        "name": "apply_kimono_style",
        "description": "應用日式和服風格。增強顏色、添加暖色調和日式邊框。",
        "parameters": {
            "type": "object",
            "properties": {
                "input_path": {
                    "type": "string",
                    "description": "輸入圖片路徑"
                },
                "output_path": {
                    "type": "string",
                    "description": "輸出圖片路徑（可選）"
                }
            },
            "required": ["input_path"]
        }
    },
    handler=lambda args, **kw: apply_kimono_style(
        input_path=args.get("input_path", ""),
        output_path=args.get("output_path")
    ),
    check_fn=check_requirements,
)

print("✅ Image Processing tools registered successfully!")
