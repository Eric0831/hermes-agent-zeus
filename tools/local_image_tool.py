#!/usr/bin/env python3
"""
Local Image Generation Tool for HERMES
完全免費，使用本地 Python 庫生成圖像
不需要任何外部 API
"""
import json
import requests
from tools.registry import registry

def check_requirements() -> bool:
    """Check if the local image server is running"""
    try:
        response = requests.get("http://localhost:8888/health", timeout=2)
        return response.status_code == 200
    except:
        return False

def local_image_generate(
    theme: str = "abstract",
    width: int = 512,
    height: int = 512,
    task_id: str = None
) -> str:
    """
    Generate an image locally using Python PIL
    
    Args:
        theme: Image theme (skull, cat, dragon, heart, abstract, geometric, nature, fire)
        width: Image width in pixels
        height: Image height in pixels
        task_id: Optional task ID
    
    Returns:
        JSON string with image path and metadata
    """
    try:
        # Send request to local image generation server
        response = requests.post(
            "http://localhost:8888/",
            json={
                "theme": theme,
                "width": width,
                "height": height
            },
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            if result.get("success"):
                return json.dumps({
                    "success": True,
                    "message": f"✅ 圖像已生成：{theme} ({width}x{height})",
                    "image_path": result["image_path"],
                    "theme": theme,
                    "width": width,
                    "height": height,
                    "media": f"MEDIA:{result['image_path']}"
                }, ensure_ascii=False, indent=2)
            else:
                return json.dumps({
                    "success": False,
                    "error": result.get("error", "Unknown error")
                }, ensure_ascii=False, indent=2)
        else:
            return json.dumps({
                "success": False,
                "error": f"HTTP {response.status_code}: {response.text}"
            }, ensure_ascii=False, indent=2)
            
    except requests.exceptions.ConnectionError:
        return json.dumps({
            "success": False,
            "error": "❌ 本地圖像服務未運行。請先啟動服務：python3 /home/testai/.hermes/local-image-gen-server.py",
            "hint": "使用命令：nohup python3 /home/testai/.hermes/local-image-gen-server.py &"
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e)
        }, ensure_ascii=False, indent=2)

# Register the tool
registry.register(
    name="local_image_generate",
    toolset="local-image",
    schema={
        "name": "local_image_generate",
        "description": """Generate images locally using Python PIL - completely free, no API required.
Available themes: skull, cat, dragon, heart, abstract, geometric, nature, fire.
Returns a PNG image file that can be sent via MEDIA: path.""",
        "parameters": {
            "type": "object",
            "properties": {
                "theme": {
                    "type": "string",
                    "description": "Image theme: skull, cat, dragon, heart, abstract, geometric, nature, fire",
                    "enum": ["skull", "cat", "dragon", "heart", "abstract", "geometric", "nature", "fire"]
                },
                "width": {
                    "type": "integer",
                    "description": "Image width in pixels (default: 512)",
                    "default": 512
                },
                "height": {
                    "type": "integer",
                    "description": "Image height in pixels (default: 512)",
                    "default": 512
                }
            },
            "required": []
        }
    },
    handler=lambda args, **kw: local_image_generate(
        theme=args.get("theme", "abstract"),
        width=args.get("width", 512),
        height=args.get("height", 512),
        task_id=kw.get("task_id")
    ),
    check_fn=check_requirements,
    requires_env=[],
)

print("✅ local_image_generate tool registered")
