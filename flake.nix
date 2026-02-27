{
  description = "camera-mock: Multi-stream ONVIF camera simulator with RTSP feeds";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };

        # Python with GStreamer bindings and optional deps
        pythonEnv = pkgs.python3.withPackages (ps: [
          ps.pygobject3
          ps.pyyaml
        ]);

        # C build dependencies for wsdd and onvif_srvd
        buildDeps = with pkgs; [
          gcc
          gnumake
          flex
          bison
          byacc
          m4
          autoconf
          unzip
          wget
          pkg-config
        ];

        # GStreamer runtime plugins
        gstPlugins = with pkgs.gst_all_1; [
          gstreamer
          gst-plugins-base
          gst-plugins-good
          gst-plugins-bad
          gst-plugins-ugly
          gst-rtsp-server
        ];

        # Build wsdd and onvif_srvd from source
        onvif-binaries = pkgs.stdenv.mkDerivation {
          pname = "camera-mock-onvif";
          version = "1.0.0";
          src = ./.;

          nativeBuildInputs = buildDeps;

          buildPhase = ''
            cd wsdd && make release && cd ..
            cd onvif_srvd && make release && cd ..
          '';

          installPhase = ''
            mkdir -p $out/bin
            cp wsdd/wsdd $out/bin/ || true
            cp onvif_srvd/onvif_srvd $out/bin/ || true
          '';

          # gsoap downloads from sourceforge during build — needs network
          # In a proper Nix build, we'd pre-fetch this. For now, use impure.
          __noChroot = true;
        };

        # Wrapper script that sets up the environment
        camera-mock = pkgs.writeShellApplication {
          name = "camera-mock";
          runtimeInputs = [ pythonEnv pkgs.iproute2 ] ++ gstPlugins;

          text = ''
            export GI_TYPELIB_PATH="${pkgs.lib.makeSearchPath "lib/girepository-1.0" gstPlugins}"
            export GST_PLUGIN_PATH="${pkgs.lib.makeSearchPath "lib/gstreamer-1.0" gstPlugins}"
            export DIRECTORY="${onvif-binaries}/bin/.."

            exec ${pythonEnv}/bin/python3 ${./main.py} "$@"
          '';
        };

      in {
        packages = {
          default = camera-mock;
          onvif-binaries = onvif-binaries;
        };

        # nix develop — drop into a shell with all deps
        devShells.default = pkgs.mkShell {
          packages = [
            pythonEnv
            pkgs.iproute2
          ] ++ gstPlugins ++ buildDeps;

          shellHook = ''
            export GI_TYPELIB_PATH="${pkgs.lib.makeSearchPath "lib/girepository-1.0" gstPlugins}"
            export GST_PLUGIN_PATH="${pkgs.lib.makeSearchPath "lib/gstreamer-1.0" gstPlugins}"
            echo "camera-mock dev shell ready"
            echo "  Run: python3 main.py --config config.yaml"
          '';
        };
      }
    ) // {
      # Home Manager module for Pi OS + Determinate Nix + Home Manager
      homeManagerModules.default = { config, lib, pkgs, ... }:
        let
          cfg = config.services.camera-mock;
          settingsFormat = pkgs.formats.yaml {};
          configFile = settingsFormat.generate "camera-mock-config.yaml" cfg.settings;
        in {
          options.services.camera-mock = {
            enable = lib.mkEnableOption "camera-mock RTSP simulator";

            package = lib.mkOption {
              type = lib.types.package;
              default = self.packages.${pkgs.system}.default;
              description = "camera-mock package to use";
            };

            settings = lib.mkOption {
              type = settingsFormat.type;
              default = {};
              description = "camera-mock YAML configuration (see config.example.yaml)";
            };

            environmentFile = lib.mkOption {
              type = lib.types.nullOr lib.types.path;
              default = null;
              description = "Environment file with secrets (e.g., JELLYFIN_API_KEY)";
            };
          };

          config = lib.mkIf cfg.enable {
            # User-level systemd service (no root needed for port 8554)
            systemd.user.services.camera-mock = {
              Unit = {
                Description = "camera-mock RTSP simulator";
                After = [ "network-online.target" ];
              };
              Service = {
                ExecStart = "${cfg.package}/bin/camera-mock --config ${configFile}";
                Restart = "on-failure";
                RestartSec = 5;
                EnvironmentFile = lib.mkIf (cfg.environmentFile != null) cfg.environmentFile;
              };
              Install = {
                WantedBy = [ "default.target" ];
              };
            };

            # Add package to user profile
            home.packages = [ cfg.package ];
          };
        };
    };
}
