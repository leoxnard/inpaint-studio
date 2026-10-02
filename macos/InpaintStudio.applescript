-- Inpaint Studio launcher (stay-open applet). The app code is bundled in Contents/Resources/app.
-- Opening the app installs uv + the app's Python deps on first start, starts the local server and
-- opens the UI (which shows the setup page if ComfyUI or models are missing). The server starts
-- ComfyUI headless when needed. Quitting stops the server, which also stops that ComfyUI.

property appPort : "7380"
property supportDir : "$HOME/Library/Application Support/Inpaint Studio"
property logFile : "$HOME/Library/Logs/InpaintStudio.log"

on run
	if not serverUp() then
		set appDir to POSIX path of (path to me) & "Contents/Resources/app"
		set envCmd to "export PATH=\"$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH\"" & ¬
			" UV_PROJECT_ENVIRONMENT=\"" & supportDir & "/venv\" && mkdir -p \"" & supportDir & "\" \"$HOME/Library/Logs\"" & ¬
			" && cd " & quoted form of appDir
		set progress total steps to 3
		set progress completed steps to 0
		set progress description to "Starting Inpaint Studio"
		try
			if not hasUv() then
				set progress additional description to "Installing uv (Python tool manager)…"
				do shell script "curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh > /dev/null 2>&1"
			end if
			set progress completed steps to 1
			set progress additional description to "Installing app dependencies (first start can take a minute)…"
			do shell script envCmd & " && uv sync --frozen --no-dev -q >> \"" & logFile & "\" 2>&1"
			set progress completed steps to 2
			set progress additional description to "Starting server…"
			-- fully detached (all streams redirected), otherwise "do shell script" waits and the app hangs
			do shell script envCmd & " && (nohup \"" & supportDir & "/venv/bin/python\" -m uvicorn server:app --host 127.0.0.1 --port " & ¬
				appPort & " >> \"" & logFile & "\" 2>&1 < /dev/null &)"
		on error errMsg
			display dialog "Inpaint Studio could not start: " & errMsg & return & return & "Log: ~/Library/Logs/InpaintStudio.log" buttons {"OK"} default button 1 with icon stop
			quit
			return
		end try
		repeat 60 times -- wait up to 30 s for the server
			if serverUp() then exit repeat
			delay 0.5
		end repeat
		set progress completed steps to 3
		if not serverUp() then
			display dialog "Inpaint Studio could not start. See ~/Library/Logs/InpaintStudio.log" buttons {"OK"} default button 1 with icon stop
			quit
			return
		end if
	end if
	open location "http://127.0.0.1:" & appPort
end run

on reopen
	-- clicking the Dock icon again just opens the UI
	open location "http://127.0.0.1:" & appPort
end reopen

on hasUv()
	try
		do shell script "export PATH=\"$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH\" && command -v uv"
		return true
	on error
		return false
	end try
end hasUv

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
		-- SIGTERM lets the server shut down cleanly; its shutdown hook stops the ComfyUI it started
		do shell script "pkill -f 'uvicorn server:app.*--port " & appPort & "' || true; " & ¬
			"for i in $(seq 1 30); do curl -s -o /dev/null --max-time 1 http://127.0.0.1:" & appPort & "/healthz || break; sleep 0.5; done"
	end try
	continue quit
end quit
