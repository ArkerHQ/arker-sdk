import {
  SandboxFilesystem,
  type ContainerProcess,
  type SandboxExecParams,
} from "modal";
import source from "./guest-filesystem.generated.js";

export function modalFilesystem(
  exec: (
    command: string[],
    params?: SandboxExecParams,
  ) => Promise<ContainerProcess>,
): SandboxFilesystem {
  const filesystem = new SandboxFilesystem((command, options) => {
    if (
      command.length !== 2 ||
      command[0] !== "/__modal/.bin/modal-sandbox-fs-tools"
    ) {
      throw new Error("Unexpected Modal filesystem command");
    }
    return exec(["python3", "-c", source, command[1]!], options);
  });
  filesystem.watch = () => {
    throw new Error(
      "Modal filesystem watching needs an ordered guest event stream on Arker",
    );
  };
  return filesystem;
}
