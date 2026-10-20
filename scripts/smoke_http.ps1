param([string]$BaseUrl = 'http://127.0.0.1:8765')
$ErrorActionPreference = 'Stop'

function Send-PhoneJson([string]$Route, [hashtable]$Body) {
    $json = $Body | ConvertTo-Json -Depth 20 -Compress
    Invoke-RestMethod -Uri "$BaseUrl$Route" -Method Post -ContentType 'application/json' `
        -Body ([System.Text.Encoding]::UTF8.GetBytes($json))
}

Send-PhoneJson '/agents/register' @{ agent_id = 'agent_a'; name = 'Agent A' } | Out-Null
Send-PhoneJson '/agents/register' @{ agent_id = 'agent_b'; name = 'Agent B' } | Out-Null
$request = Send-PhoneJson '/messages' @{
    sender_agent_id = 'agent_a'; recipient_agent_id = 'agent_b'; content = 'Please review function X.'
}
$received = Invoke-RestMethod "$BaseUrl/messages?agent_id=agent_b&conversation_id=$($request.conversation_id)"
if ($received.messages[0].id -ne $request.id) { throw 'Agent B did not receive the request' }
$reply = Send-PhoneJson '/messages' @{
    sender_agent_id = 'agent_b'; recipient_agent_id = 'agent_a'; content = 'HTTP smoke-test reply.'
    conversation_id = $request.conversation_id; reply_to_message_id = $request.id
}
$replies = Invoke-RestMethod "$BaseUrl/messages?agent_id=agent_a&conversation_id=$($request.conversation_id)"
if ($replies.messages[0].id -ne $reply.id) { throw 'Agent A did not receive the reply' }
$history = Invoke-RestMethod "$BaseUrl/conversations/$($request.conversation_id)"
if ($history.messages.Count -ne 2) { throw 'Unexpected conversation history' }
Write-Output "PASS: request and reply persisted in conversation $($request.conversation_id)"
$history | ConvertTo-Json -Depth 20
Invoke-RestMethod "$BaseUrl/events" | ConvertTo-Json -Depth 20

