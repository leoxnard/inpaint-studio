-- Inpaint Studio launcher (stay-open applet).
-- Opening the app starts ComfyUI (headless, only if no ComfyUI is running yet) and the local
-- server, then opens the UI. Quitting stops the server and the ComfyUI it started.

property projectDir : "/Users/leoxnard/code/inpaint-studio"
property appPort : "7380"
property logFile : "~/Library/Logs/InpaintStudio.log"
property comfyPort : "8188"
property comfyLog : "~/Library/Logs/InpaintStudio-ComfyUI.log"
property comfyPidFile : "~/Library/Caches/InpaintStudio-comfy.pid"

-- same setup as Comfy Desktop (venv, shared models/input/output), just without its window
on startComfy()
	set cmd to "cd \"$HOME/ComfyUI-Installs/ComfyUI\" && " & ¬
		"paths=$(ls \"$HOME/Library/Application Support/Comfy Desktop/instance-model-paths/\"*.yaml 2>/dev/null | head -1); " & ¬
		"(nohup ComfyUI/.venv/bin/python3 -s ComfyUI/main.py --port " & comfyPort & ¬
		" ${paths:+--extra-model-paths-config \"$paths\"}" & ¬
		" --input-directory \"$HOME/ComfyUI-Shared/input\" --output-directory \"$HOME/ComfyUI-Shared/output\"" & ¬
		" > " & comfyLog & " 2>&1 < /dev/null & echo $! > " & comfyPidFile & ")"
	do shell script "export PYTHONIOENCODING=utf-8; cd \"$HOME/ComfyUI-Installs/ComfyUI\" && test -x ComfyUI/.venv/bin/python3 || exit 1; " & cmd
end startComfy

on comfyUp()
	try
		do shell script "curl -s -o /dev/null -w '%{http_code}' --max-time 1 http://127.0.0.1:" & comfyPort & "/system_stats | grep -q 200"
		return true
	on error
		return false
	end try
end comfyUp

on stopComfy()
	-- only the ComfyUI this app started (pid file), never Comfy Desktop's own
	try
		do shell script "f=" & comfyPidFile & "; [ -f $f ] || exit 0; pid=$(cat $f); " & ¬
			"ps -p $pid -o args= | grep -q 'ComfyUI/main.py' && kill $pid; rm -f $f"
	end try
end stopComfy

on run
	set startedComfy to false
	if not comfyUp() then
		try
			startComfy()
			set startedComfy to true
		on error
			display dialog "ComfyUI could not be started (expected in ~/ComfyUI-Installs/ComfyUI). Start Comfy Desktop manually." buttons {"OK"} default button 1 with icon caution
		end try
	end if
	if not serverUp() then
		-- deps first (foreground), then the server fully detached: all three streams redirected,
		-- otherwise "do shell script" keeps waiting on the background process and the app hangs
		set envCmd to "cd " & quoted form of projectDir & " && export PATH=\"$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH\""
		do shell script envCmd & " && uv sync -q"
		do shell script envCmd & " && (nohup uv run uvicorn server:app --host 127.0.0.1 --port " & appPort & ¬
			" > " & logFile & " 2>&1 < /dev/null &)"
		repeat 60 times -- wait up to 30 s for the server
			if serverUp() then exit repeat
			delay 0.5
		end repeat
		if not serverUp() then
			display dialog "Inpaint Studio could not start. See " & logFile buttons {"OK"} default button 1 with icon stop
			quit
			return
		end if
	end if
	if startedComfy then
		repeat 240 times -- model lists need ComfyUI, so wait up to 2 min before opening the UI
			if comfyUp() then exit repeat
			delay 0.5
		end repeat
		if not comfyUp() then display dialog "ComfyUI is still starting. See " & comfyLog buttons {"OK"} default button 1
	end if
	open location "http://127.0.0.1:" & appPort
end run

on reopen
	-- clicking the Dock icon again just opens the UI
	open location "http://127.0.0.1:" & appPort
end reopen

on serverUp()
	try
		do shell script "curl -s -o /dev/null -w '%{http_code}' --max-time 1 http://127.0.0.1:" & appPort & "/healthz | grep -q 200"
		return true
	on error
		return false
	end try
end serverUp

on activeJobs()
	try
		return (do shell script "curl -s --max-time 2 http://127.0.0.1:" & appPort & "/api/jobs | /usr/bin/python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'") as integer
	on error
		return 0
	end try
end activeJobs

on quit
	set n to activeJobs()
	if n > 0 then
		try
			display dialog (n as text) & " job(s) are still queued or running. Quitting stops the server (and the ComfyUI it started), so they will be lost." buttons {"Keep running", "Quit anyway"} default button "Keep running" with icon caution
			if button returned of result is "Keep running" then return
		on error
			return -- dialog cancelled
		end try
	end if
	try
		do shell script "pkill -f 'uvicorn server:app.*--port " & appPort & "' || true"
	end try
	stopComfy()
	continue quit
end quit
