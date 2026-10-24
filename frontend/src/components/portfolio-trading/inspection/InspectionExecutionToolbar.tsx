import { Button, Select } from "antd";
import { CalendarDays, Workflow } from "lucide-react";

interface Props {
  date?: string; days: Array<{trade_date:string;status:string}>; loading:boolean;
  onDate:(date:string)=>void; onJob:()=>void;
}
const labels:Record<string,string>={PASSED:"核对通过",BLOCKED:"待处理",EVIDENCE_INCOMPLETE:"材料待核实",DIFF_FOUND:"账实差异",NOT_DUE:"尚未到期",NOT_CHECKED:"待核对",READ_ERROR:"读取失败"};
export default function InspectionExecutionToolbar({date,days,loading,onDate,onJob}:Props) {
  const dates=[...new Map([...(date?[{trade_date:date,status:""}]:[]),...days].map(day=>[day.trade_date,day])).values()].sort((a,b)=>a.trade_date.localeCompare(b.trade_date));
  return <div className="pi-execution-toolbar pi-execution-date-toolbar"><label><span><CalendarDays size={15} aria-hidden="true"/>核对交易日</span><Select aria-label="核对交易日" value={date} loading={loading} showSearch onChange={onDate} options={dates.map(day=>({value:day.trade_date,label:`${day.trade_date}${day.status?` · ${labels[day.status]??"待核实"}`:""}`}))}/></label><Button type="text" icon={<Workflow size={15} aria-hidden="true"/>} onClick={onJob}>自动核对作业</Button></div>;
}
