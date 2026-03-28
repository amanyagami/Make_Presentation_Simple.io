from huggingface_hub import InferenceClient
import base64
from pathlib import Path
from typing import List, Tuple, Optional
import json
import re

def _image_to_b64(path_or_url: str) -> str:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    p = Path(path_or_url)
    b = p.read_bytes()
    suffix = p.suffix.lower()
    if suffix in [".jpg", ".jpeg"]:
        mime = "image/jpeg"
    elif suffix == ".png":
        mime = "image/png"
    elif suffix == ".webp":
        mime = "image/webp"
    else:
        mime = "application/octet-stream"
    b64 = base64.b64encode(b).decode("ascii")
    return f"data:{mime};base64,{b64}"

def generate_multimodal_response(
    image_paths_or_urls: List[str],
    raw_text :str ,
    hf_token: str,
    model: str = "Qwen/Qwen3-VL-8B-Instruct",
    provider: Optional[str] = "novita",
    max_tokens: int = 5000,
    temperature: float = 0.0,
) -> Tuple[str, str]:
    """
    Sends the fixed JSON-only instruction plus images to a multimodal model and returns:
      (thinking_text, final_json_string)

    - image_paths_or_urls: list of local paths or http(s) URLs
    - final_json_string: validated JSON (string) following the user's slide schema
    """
    client = InferenceClient(token=hf_token, provider=provider)

    images_payload = [_image_to_b64(p) for p in image_paths_or_urls]
    images_placeholders = [f"<Image {i+1}>" for i in range(len(images_payload))]

    # Fixed instruction (exact structure requested by the user)
    user_instruction = (
    "How many slides are required to explain the full story of the text? "
    + "text = "
    + " :".join(raw_text)
    + " :\n"
    "Understand the text fully and create two kinds of slides:\n"
    "type1: without image\n"
    "type2: with images\n"
    "Both slide types must contain content.\n"
    "If a slide uses an image, it must explain the image and its impact.\n"
    "All images must be used and explained in the slide flow.\n"
    "The slides should follow a flow that explains the full story.\n"
    "Each slide's content should be concise.\n"
    "Explain impact in 3-4 bullet points inside the slide object's steps as separate step entries.\n"
    "The slides should start with a title-only slide and end with a thank-you slide.\n"
    "Output must strictly follow the JSON slide structure below.\n\n"

    "Slide structure:\n"
    "{\n"
    "  \"slides\": [\n"
    "    {\n"
    "      \"id\": \"slide1\",\n"
    "      \"order\": 1,\n"
    "      \"type\": \"content | image\",\n"
    "      \"title\": \"Slide title\",\n"
    "      \"subtitle\": null,\n"
    "      \"image_ref\": null,\n"
    "      \"notes\": \"Short explanation or speaker notes\",\n"
    "      \"steps\": [\n"
    "        {\n"
    "          \"number\": 1,\n"
    "          \"heading\": \"Point heading\",\n"
    "          \"content\": \"Concise explanation\"\n"
    "        }\n"
    "      ]\n"
    "    }\n"
    "  ]\n"
    "}\n\n"

    "Rules:\n"
    "- 'type' must be either 'content' or 'image'\n"
    "- Title slide must have empty steps array\n"
    "- Thank you slide should have 1 step only\n"
    "- Each normal slide must have 3-4 steps\n"
    "- Keep content concise and meaningful\n\n"

    "Map images to placeholders in order: " + ", ".join(images_placeholders) + ".\n"
    "When referencing an image in a slide object, set \"image_ref\" to the placeholder string "
    "(for example, \"<Image 1>\").\n"
    "Return JSON only.\n"
)

    # Message content: text part + image parts
    user_content_parts = [{"type": "text", "text": user_instruction}]
    for i, img in enumerate(images_payload):
        user_content_parts.append({
            "type": "image_url",
            "image_url": {"url": img, "alt": f"Image {i+1}"}
        })

    messages = [
        {"role": "system", "content": "You are a helpful instruction-following assistant that can see and reason about images. STRICTLY RETURN JSON ONLY when requested."},
        {"role": "user", "content": user_content_parts}
    ]

    resp = client.chat_completion(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    # extract raw text output from response (handle common SDK shapes)
    raw_output = None
    try:
        raw_output = resp.choices[0].message.content
    except Exception:
        raw_output = getattr(resp, "output_text", None) or getattr(resp, "output", None)
        if isinstance(raw_output, list):
            raw_output = " ".join([p.get("text","") if isinstance(p, dict) else str(p) for p in raw_output])
    if raw_output is None:
        raise RuntimeError("Unable to read text output from InferenceClient response object. Inspect `resp` for shape.")

    # split internal <think> if present
    thinking = ""
    final_text = raw_output
    if "</think>" in raw_output:
        thinking, final_text = raw_output.rsplit("</think>", 1)
        thinking = thinking.replace("<think>", "").strip()
        final_text = final_text.strip()

    # Validate/clean model output into pure JSON:
    json_text = None
    try:
        parsed = json.loads(final_text)
        json_text = json.dumps(parsed, ensure_ascii=False)
    except Exception:
        m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", final_text)
        if m:
            candidate = m.group(1)
            # Try to parse progressively shorter substrings (simple heuristic)
            for end in range(len(candidate), 0, -1):
                try:
                    parsed = json.loads(candidate[:end])
                    json_text = json.dumps(parsed, ensure_ascii=False)
                    break
                except Exception:
                    continue
        if json_text is None:
            diagnostic = {
                "error": "model_output_not_valid_json",
                "raw_output": final_text
            }
            json_text = json.dumps(diagnostic, ensure_ascii=False)

    return thinking, json_text

# vlm_query.py (append)

from pathlib import Path
import re
import json
from typing import List, Tuple

def generate_multimodal_slides(
    image_paths_or_urls: List[str],
    hf_token: str,
    raw_text: str,
    model: str = "Qwen/Qwen3-VL-8B-Instruct",
    provider: Optional[str] = "novita",
    max_tokens: int = 5000,
    temperature: float = 0.0,
) -> Tuple[str, str]:
    """
    Wrapper around generate_multimodal_response that:
      - calls the multimodal model,
      - parses the returned JSON,
      - replaces any image placeholders like "<Image 1>" in slide.image_ref
        with corresponding /uploads/figures/<basename> web paths,
      - returns (thinking, final_json_string) where final_json_string is JSON text.

    image_paths_or_urls: list of local absolute paths or http(s) URLs (in order).
                         For local files saved under your app, pass absolute filesystem paths.
    Replacement web path logic:
      - If a provided path contains '/uploads/figures/' it will use the substring starting at '/uploads/figures/'.
      - Otherwise it defaults to '/uploads/figures/<basename>' as a best-effort mapping.
    """
    thinking, json_text = generate_multimodal_response(
        image_paths_or_urls=image_paths_or_urls,
        raw_text=raw_text,
        hf_token=hf_token,
        model=model,
        provider=provider,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    # Parse the JSON from the model (it should already be JSON or diagnostic JSON)
    try:
        data = json.loads(json_text)
    except Exception as e:
        # If model returned a diagnostic JSON (see generate_multimodal_response), just return as-is
        return thinking, json_text

    # Build web-path mapping in order (index 0 -> Image 1)
    web_paths: List[str] = []
    for p in image_paths_or_urls:
        sp = str(p)
        # prefer explicit /uploads/figures/ occurrences
        idx = sp.find("/uploads/figures/")
        if idx != -1:
            web_paths.append(sp[idx:])  # /uploads/figures/...
            continue
        # if user already passed a web path
        if sp.startswith("/uploads/"):
            web_paths.append(sp)
            continue
        # fallback to basename under /uploads/figures/
        web_paths.append("/uploads/figures/" + Path(sp).name)

    # Replace placeholders in slide.image_ref fields where they match "<Image N>" (case-insensitive)
    placeholder_re = re.compile(r"<\s*Image\s*(\d+)\s*>", flags=re.IGNORECASE)
    slides = data.get("slides", [])
    for s in slides:
        img_ref = s.get("image_ref")
        if isinstance(img_ref, str):
            m = placeholder_re.search(img_ref)
            if m:
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(web_paths):
                    s["image_ref"] = web_paths[idx]
                else:
                    # out of range — leave original or set to None
                    s["image_ref"] = None

    # Return normalized JSON string
    final_json = json.dumps(data, ensure_ascii=False)
    return thinking, final_json