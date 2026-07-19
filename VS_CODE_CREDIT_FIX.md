# 🔧 VS Code Claude Code — Credit Fix Guide

## ⚡ The Problem
If `ANTHROPIC_API_KEY` is set anywhere, Claude Code charges you **API rates** ($1–$25 per million tokens) instead of using your **subscription**. This drains credits rapidly.

---

## ✅ Step 1: Remove API Key from Environment

### Check if API Key is Set:
```bash
echo $ANTHROPIC_API_KEY
```

If output shows a key: **IT'S BLEEDING YOUR CREDITS** ❌

### Remove It Permanently:

**For macOS/Linux (Bash):**
```bash
# Edit your shell config
nano ~/.bashrc    # or ~/.zshrc (for zsh)

# Find and DELETE this line (if present):
export ANTHROPIC_API_KEY="sk-ant-..."

# Save (Ctrl+O, Enter, Ctrl+X)
# Reload shell:
source ~/.bashrc
```

**For macOS (Zsh - most common now):**
```bash
nano ~/.zshrc

# Delete this line:
export ANTHROPIC_API_KEY="sk-ant-..."

# Save and reload:
source ~/.zshrc
```

### Verify It's Gone:
```bash
echo "API Key set: $([ -n "$ANTHROPIC_API_KEY" ] && echo 'YES ❌' || echo 'NO ✓')"
```

---

## ✅ Step 2: Configure VS Code for Subscription Mode

### Open VS Code Settings:
1. Press `Cmd+,` (Mac) or `Ctrl+,` (Windows/Linux)
2. Search: `claude`
3. Look for **"Claude API Key"** setting

### Remove Any API Key from Settings:
- If you see a key stored → **DELETE IT**
- Leave the field **empty**

### Check Authentication Method:
1. Open VS Code Command Palette: `Cmd+Shift+P`
2. Type: `Claude Code: Account`
3. You should see: **"Signed in as: [your email]"**
4. This confirms you're using **subscription mode** ✓

---

## ✅ Step 3: Check Your Plan & Usage

### In VS Code:
1. Open Chat panel (Claude icon on left sidebar)
2. Type: `/usage`
3. Look for:
   - **Plan type**: Should show "Pro" or "Max" (not "API Credits")
   - **Weekly usage**: Shows remaining quota
   - **Flagged behaviors**: Highlights what's consuming the most

### If It Shows "API Credits":
- Step 1 didn't work; check other config locations:
  ```bash
  # Check .env files
  find ~ -name ".env" -type f 2>/dev/null | xargs grep ANTHROPIC_API_KEY

  # Check VS Code workspace settings
  cat .vscode/settings.json 2>/dev/null | grep -i anthropic

  # Check global VS Code settings
  cat ~/.config/Code/User/settings.json 2>/dev/null | grep -i anthropic
  ```

---

## ✅ Step 4: Optimize to Save Credits

### Model Selection (Big Savings):
```
/model claude-sonnet-5   # Medium cost, fast (recommended)
/model claude-haiku-4.5  # Cheapest, good for quick tasks
/model claude-opus-4.8   # Most expensive, use sparingly
```

### Disable Expensive Features:
- **Extended thinking**: Multiplies token usage by 3-5x
  - Disable in VS Code settings: Search `extended thinking` → uncheck
- **Parallel subagents**: Each agent duplicates context
  - Consolidate tasks into fewer agents

### Context Management:
- **Don't @-mention entire folders**: Reference specific files only
- **Trim CLAUDE.md**: Remove architecture docs; keep only gotchas
- **Disable unused MCP servers**: `/mcp` to manage connections

---

## ✅ Step 5: Verify the Fix

### Quick Check:
```bash
# Close VS Code completely
pkill -f "Code"

# Wait 2 seconds
sleep 2

# Reopen and check:
# 1. Command Palette → "Claude Code: Account"
# 2. Should show your email (subscription mode)
# 3. Type `/usage` → should show "Pro" or "Max", not "API Credits"
```

### Expected Results ✓:
- `/usage` shows **subscription plan** (not "API Credits")
- No API key in `echo $ANTHROPIC_API_KEY`
- No API key in VS Code settings
- Signed in with your Claude account email

---

## 🚨 Common Mistakes

| Mistake | Fix |
|---------|-----|
| API key in `.bashrc` but shell is `zsh` | Edit `~/.zshrc` instead |
| API key in VS Code settings workspace | Delete from `.vscode/settings.json` |
| Multiple terminals with different shells | Remove from ALL shell config files |
| Using `npm start` script with API key | Check `package.json` start script |
| API key in `.env.local` | Rename to `.env.local.bak` to disable |

---

## 📋 Checklist Before You're Done

- [ ] Ran `echo $ANTHROPIC_API_KEY` → **empty output**
- [ ] Searched VS Code settings for "claude" → **no API key field**
- [ ] Ran `/usage` in VS Code → **shows "Pro" or "Max" plan**
- [ ] Signed in as your email (not API key)
- [ ] Changed model to Sonnet (`/model claude-sonnet-5`)
- [ ] Disabled extended thinking in settings
- [ ] Closed and reopened VS Code to apply changes

✅ You're done! Credits should stop bleeding now.

---

## 💬 Still Seeing API Charges?

If `/usage` still shows "API Credits":

1. **Check organization accounts**: If you have multiple Claude accounts, make sure you're signed into the right one
2. **Check GitHub authentication**: VS Code might be pulling API key from GitHub credentials
3. **Contact Anthropic support**: Provide screenshot of `/usage` output

