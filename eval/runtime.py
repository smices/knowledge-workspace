import json, platform, subprocess

def main():
    result = {'platform': platform.platform(), 'ollama': None, 'metal': 'unknown'}
    try:
        result['ollama'] = subprocess.check_output(['ollama', 'ps'], text=True, timeout=10)
        result['metal'] = 'automatic-on-macOS-if-Ollama-uses-GPU-column'
    except Exception as exc:
        result['error'] = str(exc)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__': main()
