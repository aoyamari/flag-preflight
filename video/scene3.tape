# Flag Preflight demo — scene 3: blocked by a running experiment (Development, read-only)
# Real Claude Code session, recorded headless with VHS. Run from the repo root:
#   env -i HOME=$HOME PATH=$PATH TERM=xterm-256color LANG=C.UTF-8 VHS_NO_SANDBOX=true vhs video/scene3.tape
# (a clean environment, so the session starts in the default permission mode).
# video/build.sh cuts, speeds up (labelled) and narrates the raw recording.
Output "video/raw/scene3.mp4"
Require claude
Set Shell "bash"
Set FontFamily "JetBrains Mono, Symbola, Noto Sans Symbols, Noto Sans Symbols2, DejaVu Sans Mono"
Set FontSize 27
Set LineHeight 1.1
Set Width 1920
Set Height 960
Set Padding 24
Set Theme "Dracula"
Set TypingSpeed 45ms
Set Framerate 10
Set WaitTimeout 420s

Hide
Type "export PS1='$ '; clear; date +%s.%N > video/raw/scene3.t0"
Enter
Sleep 300ms
Show
Type "claude --model claude-opus-5-5 --permission-mode default --verbose"
Enter
Wait+Screen /\? for shortcuts/
Sleep 1.5s

Type "turn off checkout_layout for beta users in Development"
Sleep 600ms
Enter
Wait+Screen@60s /esc to interrupt/
Wait+Screen /\? for shortcuts/
Sleep 4s

Hide
Ctrl+C
Ctrl+C
Sleep 1.5s
Type "date +%s.%N > video/raw/scene3.t1"
Enter
Sleep 500ms
