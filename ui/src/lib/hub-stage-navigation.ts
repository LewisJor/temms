import { useRef, useState } from "react";
import type { HubStage, WorkflowTarget } from "./workbench-types";

export function useHubStageNavigation() {
  const [focusedWorkflow, setFocusedWorkflow] = useState<WorkflowTarget | undefined>();
  const [activeHubStage, setActiveHubStage] = useState<HubStage>("mission");
  const stageFlowRef = useRef<HTMLDivElement>(null);
  const ddilWorkflowRef = useRef<HTMLElement>(null);
  const modelWorkflowRef = useRef<HTMLElement>(null);
  const deploymentWorkflowRef = useRef<HTMLElement>(null);
  const plansWorkflowRef = useRef<HTMLElement>(null);
  const rolloutsWorkflowRef = useRef<HTMLElement>(null);
  const evidenceWorkflowRef = useRef<HTMLElement>(null);
  const assetsWorkflowRef = useRef<HTMLDetailsElement>(null);

  const workflowRefs = {
    model: modelWorkflowRef,
    deployment: deploymentWorkflowRef,
    plans: plansWorkflowRef,
    rollouts: rolloutsWorkflowRef,
    ddil: ddilWorkflowRef,
    evidence: evidenceWorkflowRef,
    assets: assetsWorkflowRef
  };

  function workflowClass(target: WorkflowTarget, className: string): string {
    return focusedWorkflow === target ? `${className} workflow-target-active` : className;
  }

  function workflowRefForTarget(target: WorkflowTarget) {
    return workflowRefs[target];
  }

  function navigateHubStage(stage: HubStage, options: { workflowTarget?: WorkflowTarget } = {}): void {
    setActiveHubStage(stage);
    setFocusedWorkflow(options.workflowTarget);
    window.setTimeout(() => {
      const section = options.workflowTarget ? workflowRefForTarget(options.workflowTarget).current : stageFlowRef.current;
      if (!section) return;
      section.scrollIntoView({ behavior: "smooth", block: "start" });
      if (options.workflowTarget) {
        section.focus({ preventScroll: true });
        return;
      }
      const activeStep = stageFlowRef.current?.querySelector<HTMLElement>(`[data-stage-id="${stage}"]`);
      activeStep?.focus({ preventScroll: true });
    }, 0);
  }

  return {
    activeHubStage,
    assetsWorkflowRef,
    ddilWorkflowRef,
    deploymentWorkflowRef,
    evidenceWorkflowRef,
    focusedWorkflow,
    modelWorkflowRef,
    navigateHubStage,
    plansWorkflowRef,
    rolloutsWorkflowRef,
    setFocusedWorkflow,
    stageFlowRef,
    workflowClass
  };
}
