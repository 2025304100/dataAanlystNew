param(
    [Parameter(Mandatory=$true)][ValidateSet('Register','Run','Query','Remove','Disable')][string]$Action,
    [Parameter(Mandatory=$true)][string]$RequestPath
)
$ErrorActionPreference = 'Stop'
try {
    $request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($request.task_name -notmatch '^ValidationServices-[a-f0-9]{12}$') { throw 'Unexpected task name' }
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    $uid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $root = $service.GetFolder([string][char]92)
    try { $folder = $service.GetFolder('\DataAanlystNew') }
    catch {
        if ($Action -ne 'Register') { throw }
        $folder = $root.CreateFolder('DataAanlystNew')
    }
    $task = $null
    try { $task = $folder.GetTask($request.task_name) } catch {}
    if ($task) {
        $principalSid = $task.Definition.Principal.UserId
        if ($principalSid -notmatch '^S-1-') {
            $principalSid = ([Security.Principal.NTAccount]::new($principalSid)).Translate([Security.Principal.SecurityIdentifier]).Value
        }
        if ($task.Definition.RegistrationInfo.Description -cne $request.description -or
            $task.Definition.Actions.Count -ne 1 -or
            $task.Definition.Actions.Item(1).Path -ine $request.executable -or
            $task.Definition.Actions.Item(1).Arguments -cne $request.arguments -or
            $principalSid -ine $uid -or
            $task.Definition.Principal.RunLevel -ne 0 -or
            $task.Definition.Principal.LogonType -ne 3 -or
            $task.Definition.Triggers.Count -ne 0 -or
            $task.Definition.Settings.ExecutionTimeLimit -ne 'PT0S') {
            throw 'Task name is occupied by an unconfirmed owner; preserve it'
        }
    }
    switch ($Action) {
        'Register' {
            if (-not $task) {
                [xml]$xml = $request.xml
                $ns = New-Object Xml.XmlNamespaceManager($xml.NameTable)
                $ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
                $xml.SelectSingleNode('//t:UserId',$ns).InnerText = $uid
                # Interactive token, least privilege, no stored password or trigger.
                $task = $folder.RegisterTask($request.task_name,$xml.OuterXml,6,$uid,$null,3,$null)
            }
        }
        'Run' {
            if (-not $task) { throw 'Managed task is not registered' }
            $task.Enabled = $true
            if ($task.State -ne 4) { $null = $task.Run($null) }
        }
        'Disable' {
            if (-not $task) { throw 'Managed task is not registered' }
            if ($task.State -eq 4) { throw 'Stop the protected host before disabling it' }
            $task.Enabled = $false
        }
        'Remove' {
            if (-not $task) { throw 'Managed task is not registered' }
            if ($task.State -eq 4) { throw 'Stop the protected host before removal' }
            $folder.DeleteTask($request.task_name,0)
            $task = $null
        }
    }
    [ordered]@{ok=$true; action=$Action; registered=($null -ne $task);
        state=$(if ($task) {$task.State} else {$null});
        last_result=$(if ($task) {$task.LastTaskResult} else {$null})} | ConvertTo-Json -Compress
}
catch {
    [ordered]@{ok=$false; action=$Action; error_type=$_.Exception.GetType().Name;
        hresult=$_.Exception.HResult; message=$_.Exception.Message} | ConvertTo-Json -Compress
    exit 1
}
