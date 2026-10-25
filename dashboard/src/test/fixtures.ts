import type { Snapshot, Target, WireMessage } from "@/lib/types"

export function fixture():Snapshot {
  const at=new Date().toISOString(),target:Target={cluster_id:"sentinel-lab",namespace:"sentinel-demo",kind:"Deployment",name:"demo-api",uid:"demo-uid"}
  const common={schema_version:"1.0",incident_id:"INC-001",action_id:"ACT-001",target}
  const messages:WireMessage[]=[
    {...common,kind:"outcome",status:"RECOVERED",window_start:at,window_end:at,checks:[{name:"service_health",passed:true,detail:"Healthy samples over the required window"}]},
    {...common,kind:"execution_result",status:"APPLIED",resource_version:"103",recorded_at:at,details:"Synthetic execution result"},
    {...common,schema_version:"2.0",kind:"guard_result",verdict:"ALLOW",reason_codes:["VALIDATION_PASSED"],policy_version:"demo-v1",validated_resource_version:"102",checked_at:at,valid_until:at,proposal_sha256:"a".repeat(64),inputs_sha256:"b".repeat(64)},
    {...common,kind:"dependency_result",status:"CLEAR",affected_services:["demo-api","mock-db"],blockers:[],unknowns:[],checked_at:at},
    {...common,kind:"proposal",action:"ROLLBACK",based_on_resource_version:"102",parameters:{baseline_id:"BASE-001",template_sha256:"a".repeat(64)},rationale:"Restore the observed healthy template",evidence_refs:["E-POD-001"]},
    {...common,kind:"incident",observed_at:at,signals:[{reason:"CrashLoopBackOff",source:"pod_status",detail:"Persistent restart failure",at}]},
  ]
  const sources=Object.fromEntries(["nodes","pods","events","node_metrics","pod_metrics"].map(k=>[k,{status:"available"}]))
  return {
    generated_at:at,mode:"synthetic",demo_step:"Synthetic verified recovery",limit:600,
    sources:{coordinator:{connected:true},telemetry:{connected:true},cluster_metrics:{connected:true}},
    incidents:[{sequence:1,incident_id:"INC-001",target,state:"RECOVERED",active_action_id:"ACT-001"}],
    actions:[{action_id:"ACT-001",incident_id:"INC-001",proposal:messages[4],state:"RECOVERED",guard_result:messages[2],execution_result:messages[1]}],
    messages:messages.map((message,i)=>({sequence:6-i,message})),
    approvals:[{id:1,record:{action_id:"ACT-001",operator_id:"synthetic-operator",decision:"APPROVED",approved_at:at}}],
    outbox:[{id:1,topic:"executor.request",action_id:"ACT-001",delivered:1}],
    reports:[{action_id:"ACT-001",checked_at:at,proposal_sha256:"a".repeat(64),inputs_sha256:"b".repeat(64),checks:[{code:"BASELINE_HEALTH_STATUS",status:"PASS",path:"baseline.health_evidence.status",message:"Baseline has a healthy verification result",actual:"HEALTHY",expected:"HEALTHY"}]}],
    telemetry:[{sequence:1,at,component:2,event_type:"observation",summary:"Deployment status",target,metrics:{ready:1,desired:1,available:1}},{sequence:2,at,component:1,event_type:"fault",summary:"Synthetic startup fault injected",target:null,metrics:{}}],
    cluster_history:[0,1,2].map(i=>({cluster_id:"sentinel-lab",namespace:"sentinel-demo",observed_at:new Date(Date.now()-i*3000).toISOString(),sources,
      summary:{nodes_ready:1,nodes_total:1,pods_ready:1,pods_total:1,restarts:2,cpu_usage_cores:1,cpu_allocatable_cores:4,memory_usage_bytes:1024**3,memory_allocatable_bytes:8*1024**3},
      nodes:[{name:"demo-worker-1",ready:true,conditions:[{type:"Ready",status:"True"}],cpu_usage_cores:1,cpu_allocatable_cores:4,memory_usage_bytes:1024**3,memory_allocatable_bytes:8*1024**3,metrics_timestamp:at}],
      pods:[{name:"demo-api-001",namespace:"sentinel-demo",node:"demo-worker-1",phase:"Running",ready:true,containers_ready:1,containers_total:1,restarts:2,reason:null,cpu_usage_cores:.125,memory_usage_bytes:64*1024**2,metrics_timestamp:at}],
      events:[{reason:"BackOff",object:"Pod/demo-api-001",count:3,last_seen:at}],truncation:{nodes:0,pods:0,events:0}})),
  }
}
