on run argv
  set needle to item 1 of argv
  tell application "Google Chrome"
    activate
    repeat with w from 1 to count of windows
      repeat with t from 1 to count of tabs of window w
        set tabUrl to URL of tab t of window w
        if tabUrl contains needle then
          set active tab index of window w to t
          set index of window w to 1
          return tabUrl
        end if
      end repeat
    end repeat
  end tell
  return "NOT_FOUND"
end run
