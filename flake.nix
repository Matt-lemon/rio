{
  description = "Rio: a reproducible, read-only architecture preview";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; config.allowUnfree = false; };
      python = pkgs.python312;
      environment = python.withPackages (ps: with ps; [
        httpx jsonschema python-dotenv pyyaml pytest requests
      ]);
      # Exclude local settings, state, assets and unrelated services from the
      # runtime source. No credentials may be passed as derivation inputs.
      source = pkgs.lib.cleanSourceWith {
        src = ./.;
        filter = path: type:
          let rel = pkgs.lib.removePrefix ((toString ./.) + "/") (toString path);
          in if type == "directory" then
            rel == "core" || pkgs.lib.hasPrefix "core/" rel
            || builtins.elem rel [ "examples" "tests" "tools" "tools/ci" ".github" ]
          else (pkgs.lib.hasPrefix "core/" rel && pkgs.lib.hasSuffix ".py" rel)
            || builtins.elem rel [
              "LICENSE" "main.py" "rio_core.py" "rio_voice.py" "rio_agent.py"
              "rio_memory_context.py" "rio_dubbing_client.py" "rio_dubbing_tools.py"
              "rio_dubbing_auth.py" "web_search.py" "conftest.py"
              "examples/offline_demo.py"
              "tests/test_execution_core.py" "tests/test_execution_safety.py"
              "tests/test_execution_ipc.py"
              "tools/ci/license_guard.py" ".github/license-policy.json"
            ];
      };
      demo = pkgs.writeShellApplication {
        name = "rio-offline-demo";
        runtimeInputs = [ environment ];
        meta.license = pkgs.lib.licenses.gpl3Only;
        text = ''
          export PYTHONDONTWRITEBYTECODE=1
          exec python ${source}/examples/offline_demo.py "$@"
        '';
      };
      checked = name: command: pkgs.runCommand name {
        nativeBuildInputs = [ environment ];
      } ''
        export HOME="$TMPDIR/home" USERPROFILE="$TMPDIR/home"
        mkdir -m 700 "$HOME"
        export PYTHONDONTWRITEBYTECODE=1
        export PYTHONPATH="${source}:${environment}/${python.sitePackages}"
        export RIO_HOME_EXECUTION_ENABLED=0 RIO_HOME_BACKUP_EXECUTION_ENABLED=false
        export RIO_ST_CLI_CONTROL_ENABLED=0 RIO_WEB_SEARCH_ENABLED=false
        unset RIO_TOOLS_TEST_IMAGE
        cd ${source}
        ${command}
        touch "$out"
      '';
    in {
      packages.${system}.default = demo;
      apps.${system}.default = { type = "app"; program = "${demo}/bin/rio-offline-demo"; };
      devShells.${system}.default = pkgs.mkShell { packages = [ environment ]; };
      checks.${system} = {
        offline-demo = checked "rio-offline-demo-check" "python examples/offline_demo.py";
        dependency-licenses = checked "rio-dependency-license-check"
          "python tools/ci/license_guard.py --deps-only";
        execution-safety = checked "rio-execution-safety-check" ''
          python -m pytest -q -p no:cacheprovider --basetemp "$TMPDIR/t" \
            tests/test_execution_core.py tests/test_execution_safety.py tests/test_execution_ipc.py
        '';
      };
    };
}
