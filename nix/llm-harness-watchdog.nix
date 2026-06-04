{ repoPath }:
{
  systemd.user.services.llm-harness-watchdog = {
    Unit.Description = "LLM Harness watchdog";
    Service = {
      WorkingDirectory = repoPath;
      ExecStart = "${repoPath}/harness watchdog";
      Restart = "always";
      RestartSec = 5;
    };
    Install.WantedBy = [ "default.target" ];
  };
}
