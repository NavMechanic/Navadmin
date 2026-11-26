# VS Code Configuration

This file documents the recommended VS Code setup for Navadmin development.

## Extensions to Install

Install these extensions from the VS Code Marketplace:

```bash
code --install-extension ms-python.python
code --install-extension ms-python.vscode-pylance
code --install-extension ms-python.debugpy
code --install-extension ms-python.pylint
code --install-extension esbenp.prettier-vscode
code --install-extension rangav.vscode-thunder-client
code --install-extension alolant.mermaid-markdown-syntax-highlighting
code --install-extension redhat.vscode-yaml
code --install-extension mhutchie.git-graph
code --install-extension eamodio.gitlens
```

Or open the **Extensions** tab (Ctrl+Shift+X) and search for each name above.

## Settings Configuration

Create `.vscode/settings.json`:

```json
{
  "python.defaultInterpreterPath": "${workspaceFolder}/venv/bin/python",
  "python.linting.enabled": true,
  "python.linting.pylintEnabled": true,
  "python.linting.pylintArgs": ["--max-line-length=100"],
  "python.formatting.provider": "black",
  "editor.formatOnSave": true,
  "editor.defaultFormatter": "ms-python.python",
  "[python]": {
    "editor.formatOnSave": true,
    "editor.defaultFormatter": "ms-python.python",
    "editor.codeActionsOnSave": {
      "source.organizeImports": true
    }
  },
  "files.exclude": {
    "**/__pycache__": true,
    "**/.pytest_cache": true,
    "**/*.pyc": true,
    ".venv": true,
    "venv": true,
    "*.egg-info": true
  },
  "python.testing.pytestEnabled": true,
  "python.testing.pytestArgs": ["tests"],
  "mermaid.useMaxWidth": false,
  "[markdown]": {
    "editor.wordWrap": "on"
  },
  "files.watcherExclude": {
    "**/.git/objects/**": true,
    "**/.git/subtree-cache/**": true,
    "**/node_modules/**": true,
    "**/.pytest_cache/**": true,
    "**/__pycache__/**": true
  },
  "python.analysis.extraPaths": ["${workspaceFolder}"]
}
```

## Debug Configurations

Create `.vscode/launch.json`:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Python: CLI (--help)",
      "type": "python",
      "request": "launch",
      "module": "main",
      "args": ["--help"],
      "console": "integratedTerminal",
      "justMyCode": true
    },
    {
      "name": "Python: Twin Simulate",
      "type": "python",
      "request": "launch",
      "module": "main",
      "args": ["twin-simulate", "-n", "50"],
      "console": "integratedTerminal",
      "justMyCode": true
    },
    {
      "name": "Python: Streamlit Dashboard",
      "type": "python",
      "request": "launch",
      "module": "streamlit",
      "args": ["run", "dashboard/app.py"],
      "console": "integratedTerminal",
      "justMyCode": true
    },
    {
      "name": "Python: FMEA Assessment",
      "type": "python",
      "request": "launch",
      "module": "main",
      "args": ["fmea-assess"],
      "console": "integratedTerminal",
      "justMyCode": true
    },
    {
      "name": "Python: Current File",
      "type": "python",
      "request": "launch",
      "program": "${file}",
      "console": "integratedTerminal",
      "justMyCode": true
    }
  ]
}
```

## Keyboard Shortcuts (Optional)

Add to `.vscode/keybindings.json` for faster workflow:

```json
[
  {
    "key": "shift+cmd+r",
    "command": "python.execInTerminal"
  },
  {
    "key": "shift+cmd+t",
    "command": "workbench.action.terminal.new"
  }
]
```

## Debugging Tips

### Run a Single Test
Press F5 to launch the currently open file.

### Run Twin Simulation
1. Open Command Palette (Cmd+Shift+P)
2. Select "Debug: Start Debugging"
3. Choose "Python: Twin Simulate"

### Debug the Dashboard
1. Select "Python: Streamlit Dashboard"
2. Press F5
3. Open browser to http://localhost:8501

### Inspect Variables
- Set a breakpoint (click line number)
- Run with debugger
- Hover over variables to inspect values
- Use the Debug Console (Shift+Cmd+Y) for REPL

## Performance Optimizations

If Pylance is slow, disable it for large edits:
```json
"python.languageServer": "Pylance"  // or "Jedi" for faster, less accurate
```

To exclude folders from indexing:
```json
"files.watcherExclude": {
  "**/node_modules/**": true,
  "**/__pycache__/**": true
}
```

## Formatting on Save

Ensure Python files are auto-formatted:
```json
"[python]": {
  "editor.formatOnSave": true,
  "editor.defaultFormatter": "ms-python.python"
}
```

To use Black instead:
```bash
pip install black
# Then in settings.json:
"python.formatting.blackArgs": ["--line-length", "100"]
```

## Testing Integration

To run pytest from VS Code:
1. Press Cmd+Shift+P and search "Test"
2. Select "Python: Configure Tests"
3. Choose "pytest"
4. Tests will appear in the sidebar Test Explorer

## Useful Commands

| Command | Shortcut |
|---------|----------|
| Run current file | Shift+Cmd+O |
| Format document | Shift+Alt+F |
| Organize imports | Shift+Cmd+I |
| Go to definition | Cmd+Click (or F12) |
| Find references | Shift+Cmd+F12 |
| Open terminal | Ctrl+` |
| Debug: Start | F5 |
| Debug: Continue | F5 (when paused) |
| Debug: Step Over | F10 |
| Debug: Step Into | F11 |

## Troubleshooting

### "Python interpreter not found"
1. Open Command Palette (Cmd+Shift+P)
2. Search "Python: Select Interpreter"
3. Choose the venv or system Python

### "Pylance has errors"
1. Reload VS Code window (Cmd+Shift+P → "Reload Window")
2. Or disable Pylance temporarily: `"python.languageServer": "None"`

### "Module not found"
Ensure the workspace is opened at the project root:
```bash
cd Navadmin
code .
```

### "Streamlit port already in use"
Change port in dashboard/app.py or run:
```bash
streamlit run dashboard/app.py --server.port 8502
```
