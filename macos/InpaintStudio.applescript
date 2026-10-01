-- Inpaint Studio launcher (stay-open applet).
-- Opening the app starts the local server and opens the UI; quitting the app stops the server.

property projectDir : "/Users/leoxnard/code/inpaint-studio"
property appPort : "7380"
property logFile : "~/Library/Logs/InpaintStudio.log"

on run
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
			display dialog (n as text) & " job(s) are still queued or running. Quitting stops the server and they will not be recorded (ComfyUI may still finish them)." buttons {"Keep running", "Quit anyway"} default button "Keep running" with icon caution
			if button returned of result is "Keep running" then return
		on error
			return -- dialog cancelled
		end try
	end if
	try
		do shell script "pkill -f 'uvicorn server:app.*--port " & appPort & "' || true"
	end try
	continue quit
end quit
