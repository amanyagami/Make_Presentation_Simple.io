from huggingface_hub import InferenceClient
import base64
 
def generate_response(prompt, hf_token):
    """
    Sends a prompt to the Qwen Thinking model via Hugging Face InferenceClient
    and returns separated thinking and final response.
    
    Args:
        prompt (str): The user input prompt.
        hf_token (str): Your Hugging Face API token.
    
    Returns:
        tuple: (thinking_text, final_text)
    """
    client = InferenceClient(token=hf_token, provider="nscale")

    resp = client.chat_completion(
        model="Qwen/Qwen3-4B-Instruct-2507",
        messages=[
            {"role": "user", "content": prompt}
        ],
        max_tokens=5000,
        temperature=0.7,
    )

    raw_output = resp.choices[0].message.content

    # Split thinking and final answer using </think>
    if "</think>" in raw_output:
        thinking, final = raw_output.rsplit("</think>", 1)
        thinking = thinking.replace("<think>", "").strip()
        final = final.strip()
    else:
        thinking = ""
        final = raw_output.strip()
    return thinking, final

def generate_vlm_response(prompt, hf_token, image_path=None):

    client = InferenceClient(token=hf_token, provider="nscale")

    content_blocks = []

    if image_path:
        with open(image_path, "rb") as f:
            image_base64 = base64.b64encode(f.read()).decode("utf-8")

        content_blocks.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{image_base64}"
            }
        })

    content_blocks.append({
        "type": "text",
        "text": prompt
    })

    resp = client.chat_completion(
        model="Qwen/Qwen3-VL-8B-Instruct",
        messages=[{
            "role": "user",
            "content": content_blocks
        }],
        max_tokens=2000,
        temperature=0.7,
    )

    return resp.choices[0].message.content