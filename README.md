# Portable Chat Engine MVP

## Setup Instructions (Windows PowerShell)

1. **Install Python**: Ensure Python 3.10+ is installed and added to PATH.
2. **Open PowerShell** in this folder.

### 1. Create Virtual Environment
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

### 2. Install Dependencies
```powershell
pip install -r requirements.txt
```

### 3. Setup API Key
1. Copy `.env.example` to `.env`:
   ```powershell
   Copy-Item .env.example .env
   ```
2. Open `.env` in a text editor and paste your OpenAI API Key.

## Running the Engine

### Smoke Test (Verify Setup)
```powershell
python src/smoke_test.py
```
*(You will create this file in the next step)*

### Run Chat
```powershell
python src/main.py
```
*(Coming soon)*
