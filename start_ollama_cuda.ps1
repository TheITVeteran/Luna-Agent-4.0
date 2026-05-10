$env:OLLAMA_LLM_LIBRARY = "cuda_v13"
$env:OLLAMA_FLASH_ATTENTION = "1"
$env:OLLAMA_HOST = "127.0.0.1:11434"
# 8 GiB VRAM can't hold qwen3.5:4b + qwen3-vl:4b simultaneously — keep exactly one loaded so the server cleanly swaps when vision is used.
$env:OLLAMA_MAX_LOADED_MODELS = "1"

Get-Process ollama -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }

Start-Sleep -Seconds 2
& "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" serve
