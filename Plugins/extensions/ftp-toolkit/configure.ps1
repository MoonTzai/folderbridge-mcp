param(
    [Parameter(Mandatory=$true)][string]$ProfileName,
    [Parameter(Mandatory=$true)][string]$CredentialTarget
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$credSource = @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

public static class FolderBridgeFtpCredentialStore
{
    private const uint CRED_TYPE_GENERIC = 1;
    private const uint CRED_PERSIST_LOCAL_MACHINE = 2;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct CREDENTIAL
    {
        public uint Flags;
        public uint Type;
        public string TargetName;
        public string Comment;
        public long LastWritten;
        public uint CredentialBlobSize;
        public IntPtr CredentialBlob;
        public uint Persist;
        public uint AttributeCount;
        public IntPtr Attributes;
        public string TargetAlias;
        public string UserName;
    }

    [DllImport("Advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredWrite(ref CREDENTIAL credential, uint flags);

    [DllImport("Advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredRead(string target, uint type, uint flags, out IntPtr credentialPtr);

    [DllImport("Advapi32.dll", SetLastError = false)]
    private static extern void CredFree(IntPtr buffer);

    public static string[] Read(string target)
    {
        IntPtr ptr;
        if (!CredRead(target, CRED_TYPE_GENERIC, 0, out ptr))
        {
            int error = Marshal.GetLastWin32Error();
            if (error == 1168) return null;
            throw new Win32Exception(error, "Could not read Windows credential");
        }
        try
        {
            CREDENTIAL cred = (CREDENTIAL)Marshal.PtrToStructure(ptr, typeof(CREDENTIAL));
            byte[] blob = new byte[cred.CredentialBlobSize];
            if (blob.Length > 0) Marshal.Copy(cred.CredentialBlob, blob, 0, blob.Length);
            string payload = Encoding.UTF8.GetString(blob);
            Array.Clear(blob, 0, blob.Length);
            return new string[] { cred.UserName ?? "", payload };
        }
        finally
        {
            CredFree(ptr);
        }
    }

    public static void Write(string target, string username, string payloadJson)
    {
        byte[] blob = Encoding.UTF8.GetBytes(payloadJson);
        IntPtr blobPtr = IntPtr.Zero;
        try
        {
            blobPtr = Marshal.AllocCoTaskMem(blob.Length);
            Marshal.Copy(blob, 0, blobPtr, blob.Length);
            CREDENTIAL cred = new CREDENTIAL();
            cred.Type = CRED_TYPE_GENERIC;
            cred.TargetName = target;
            cred.CredentialBlobSize = (uint)blob.Length;
            cred.CredentialBlob = blobPtr;
            cred.Persist = CRED_PERSIST_LOCAL_MACHINE;
            cred.UserName = username;
            if (!CredWrite(ref cred, 0))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not save Windows credential");
            }
        }
        finally
        {
            Array.Clear(blob, 0, blob.Length);
            if (blobPtr != IntPtr.Zero) Marshal.FreeCoTaskMem(blobPtr);
        }
    }
}
'@
[void](Add-Type -TypeDefinition $credSource -Language CSharp)

$existing = $null
try { $existing = [FolderBridgeFtpCredentialStore]::Read($CredentialTarget) } catch { $existing = $null }
$existingUser = ''
$existingPayload = $null
if ($existing -and $existing.Length -ge 2) {
    $existingUser = [string]$existing[0]
    try { $existingPayload = ([string]$existing[1]) | ConvertFrom-Json } catch { $existingPayload = $null }
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "FTP Toolkit - Configure profile: $ProfileName"
$form.StartPosition = 'CenterScreen'
$form.Size = New-Object System.Drawing.Size(570, 690)
$form.MinimumSize = New-Object System.Drawing.Size(570, 690)
$form.MaximizeBox = $false
$form.FormBorderStyle = 'FixedDialog'
$form.Font = New-Object System.Drawing.Font('Segoe UI', 9)

$title = New-Object System.Windows.Forms.Label
$title.Text = "FTP / FTPS profile: $ProfileName"
$title.Font = New-Object System.Drawing.Font('Segoe UI', 14, [System.Drawing.FontStyle]::Bold)
$title.Location = New-Object System.Drawing.Point(24, 18)
$title.AutoSize = $true
[void]$form.Controls.Add($title)

$note = New-Object System.Windows.Forms.Label
$note.Text = 'Host, remote root, username and password are stored only in Windows Credential Manager. They are not written to the workspace or returned to ChatGPT.'
$note.Location = New-Object System.Drawing.Point(26, 54)
$note.Size = New-Object System.Drawing.Size(510, 48)
[void]$form.Controls.Add($note)

function Add-Label([string]$text, [int]$y) {
    $label = New-Object System.Windows.Forms.Label
    $label.Text = $text
    $label.Location = New-Object System.Drawing.Point(28, $y)
    $label.Size = New-Object System.Drawing.Size(130, 24)
    [void]$form.Controls.Add($label)
}

Add-Label 'FTP Host' 116
$hostBox = New-Object System.Windows.Forms.TextBox
$hostBox.Location = New-Object System.Drawing.Point(165, 113)
$hostBox.Size = New-Object System.Drawing.Size(360, 25)
$hostBox.Text = if ($existingPayload -and $existingPayload.host) { [string]$existingPayload.host } else { '' }
[void]$form.Controls.Add($hostBox)

Add-Label 'Port' 154
$portBox = New-Object System.Windows.Forms.NumericUpDown
$portBox.Location = New-Object System.Drawing.Point(165, 151)
$portBox.Size = New-Object System.Drawing.Size(110, 25)
$portBox.Minimum = 1
$portBox.Maximum = 65535
$portBox.Value = if ($existingPayload -and $existingPayload.port) { [decimal]$existingPayload.port } else { 21 }
[void]$form.Controls.Add($portBox)

Add-Label 'Remote root' 192
$rootBox = New-Object System.Windows.Forms.TextBox
$rootBox.Location = New-Object System.Drawing.Point(165, 189)
$rootBox.Size = New-Object System.Drawing.Size(360, 25)
$rootBox.Text = if ($existingPayload -and $existingPayload.remote_root) { [string]$existingPayload.remote_root } else { '/' }
[void]$form.Controls.Add($rootBox)

Add-Label 'Connection' 230
$modeBox = New-Object System.Windows.Forms.ComboBox
$modeBox.Location = New-Object System.Drawing.Point(165, 227)
$modeBox.Size = New-Object System.Drawing.Size(360, 25)
$modeBox.DropDownStyle = 'DropDownList'
[void]$modeBox.Items.Add('FTPS - Explicit TLS (recommended)')
[void]$modeBox.Items.Add('Plain FTP (not encrypted)')
$modeBox.SelectedIndex = if ($existingPayload -and $existingPayload.mode -eq 'ftp-plain') { 1 } else { 0 }
[void]$form.Controls.Add($modeBox)

Add-Label 'Username' 268
$userBox = New-Object System.Windows.Forms.TextBox
$userBox.Location = New-Object System.Drawing.Point(165, 265)
$userBox.Size = New-Object System.Drawing.Size(360, 25)
$userBox.Text = $existingUser
[void]$form.Controls.Add($userBox)

Add-Label 'Password' 306
$passwordBox = New-Object System.Windows.Forms.TextBox
$passwordBox.Location = New-Object System.Drawing.Point(165, 303)
$passwordBox.Size = New-Object System.Drawing.Size(360, 25)
$passwordBox.UseSystemPasswordChar = $true
[void]$form.Controls.Add($passwordBox)

$passwordHint = New-Object System.Windows.Forms.Label
$passwordHint.Text = if ($existingPayload -and $existingPayload.password) { 'Leave blank to keep the currently saved password.' } else { 'A password is required for the first configuration.' }
$passwordHint.Location = New-Object System.Drawing.Point(165, 331)
$passwordHint.Size = New-Object System.Drawing.Size(360, 22)
$passwordHint.ForeColor = [System.Drawing.Color]::DimGray
[void]$form.Controls.Add($passwordHint)

$insecureBox = New-Object System.Windows.Forms.CheckBox
$insecureBox.Text = 'Ignore TLS certificate verification errors (not recommended)'
$insecureBox.Location = New-Object System.Drawing.Point(165, 362)
$insecureBox.Size = New-Object System.Drawing.Size(370, 28)
$insecureBox.Checked = [bool]($existingPayload -and $existingPayload.insecure_tls)
$insecureBox.Enabled = ($modeBox.SelectedIndex -eq 0)
[void]$form.Controls.Add($insecureBox)
$modeBox.Add_SelectedIndexChanged({
    $insecureBox.Enabled = ($modeBox.SelectedIndex -eq 0)
    if (-not $insecureBox.Enabled) { $insecureBox.Checked = $false }
})

$proxyBox = New-Object System.Windows.Forms.CheckBox
$proxyBox.Text = 'Use local HTTP CONNECT proxy (Clash/Mihomo mixed-port)'
$proxyBox.Location = New-Object System.Drawing.Point(165, 400)
$proxyBox.Size = New-Object System.Drawing.Size(370, 28)
$proxyBox.Checked = [bool]($existingPayload -and $existingPayload.proxy_mode -eq 'http-connect')
[void]$form.Controls.Add($proxyBox)

Add-Label 'Proxy host' 438
$proxyHostBox = New-Object System.Windows.Forms.TextBox
$proxyHostBox.Location = New-Object System.Drawing.Point(165, 435)
$proxyHostBox.Size = New-Object System.Drawing.Size(220, 25)
$proxyHostBox.Text = if ($existingPayload -and $existingPayload.proxy_host) { [string]$existingPayload.proxy_host } else { '127.0.0.1' }
[void]$form.Controls.Add($proxyHostBox)

Add-Label 'Proxy port' 476
$proxyPortBox = New-Object System.Windows.Forms.NumericUpDown
$proxyPortBox.Location = New-Object System.Drawing.Point(165, 473)
$proxyPortBox.Size = New-Object System.Drawing.Size(110, 25)
$proxyPortBox.Minimum = 1
$proxyPortBox.Maximum = 65535
$proxyPortBox.Value = if ($existingPayload -and $existingPayload.proxy_port) { [decimal]$existingPayload.proxy_port } else { 7897 }
[void]$form.Controls.Add($proxyPortBox)

$proxyHostBox.Enabled = $proxyBox.Checked
$proxyPortBox.Enabled = $proxyBox.Checked
$proxyBox.Add_CheckedChanged({
    $proxyHostBox.Enabled = $proxyBox.Checked
    $proxyPortBox.Enabled = $proxyBox.Checked
})

$security = New-Object System.Windows.Forms.Label
$security.Text = 'Remote root acts as a jail for this profile. Proxy settings contain only a local host and port; FTP credentials remain in Windows Credential Manager.'
$security.Location = New-Object System.Drawing.Point(28, 520)
$security.Size = New-Object System.Drawing.Size(500, 48)
[void]$form.Controls.Add($security)

$saveButton = New-Object System.Windows.Forms.Button
$saveButton.Text = 'Save'
$saveButton.Location = New-Object System.Drawing.Point(348, 596)
$saveButton.Size = New-Object System.Drawing.Size(85, 32)
[void]$form.Controls.Add($saveButton)

$cancelButton = New-Object System.Windows.Forms.Button
$cancelButton.Text = 'Cancel'
$cancelButton.Location = New-Object System.Drawing.Point(440, 596)
$cancelButton.Size = New-Object System.Drawing.Size(85, 32)
$cancelButton.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
[void]$form.Controls.Add($cancelButton)
$form.CancelButton = $cancelButton

$script:saved = $false
$saveButton.Add_Click({
    try {
        $hostValue = $hostBox.Text.Trim()
        $remoteRoot = $rootBox.Text.Trim()
        $username = $userBox.Text.Trim()
        $password = $passwordBox.Text
        if ($hostValue -notmatch '^[A-Za-z0-9.-]+$' -or $hostValue.Contains(':')) { throw 'FTP Host is invalid.' }
        if (-not $remoteRoot.StartsWith('/') -or $remoteRoot.Contains('..') -or $remoteRoot.Contains('\') -or $remoteRoot.IndexOfAny([char[]]"`r`n`0?#") -ge 0) { throw 'Remote root must be a safe absolute FTP path, for example / or /htdocs.' }
        if ([string]::IsNullOrWhiteSpace($username) -or $username.IndexOfAny([char[]]":`r`n`0") -ge 0) { throw 'Username is empty or contains unsupported characters.' }
        if ([string]::IsNullOrEmpty($password)) {
            if ($existingPayload -and -not [string]::IsNullOrEmpty([string]$existingPayload.password)) {
                $password = [string]$existingPayload.password
            } else {
                throw 'A password is required for the first configuration.'
            }
        }
        if ($password.IndexOfAny([char[]]"`r`n`0") -ge 0 -or $password.Length -gt 512) { throw 'Password contains unsupported characters or is too long.' }

        $mode = if ($modeBox.SelectedIndex -eq 1) { 'ftp-plain' } else { 'ftps-explicit' }
        $proxyMode = if ($proxyBox.Checked) { 'http-connect' } else { 'none' }
        $proxyHost = $proxyHostBox.Text.Trim()
        if ($proxyBox.Checked -and ($proxyHost -notmatch '^[A-Za-z0-9.-]+$' -or $proxyHost.Contains(':'))) { throw 'Proxy host is invalid.' }
        $payload = [ordered]@{
            schema = 1
            mode = $mode
            host = $hostValue
            port = [int]$portBox.Value
            remote_root = $remoteRoot
            insecure_tls = [bool]($mode -eq 'ftps-explicit' -and $insecureBox.Checked)
            proxy_mode = $proxyMode
            proxy_host = if ($proxyBox.Checked) { $proxyHost } else { '' }
            proxy_port = if ($proxyBox.Checked) { [int]$proxyPortBox.Value } else { 0 }
            password = $password
        }
        $json = $payload | ConvertTo-Json -Compress
        [FolderBridgeFtpCredentialStore]::Write($CredentialTarget, $username, $json)
        $script:saved = $true
        $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
        $form.Close()
    } catch {
        [void][System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Could not save FTP profile', 'OK', 'Error')
    }
})

$result = $form.ShowDialog()
if ($script:saved -and $result -eq [System.Windows.Forms.DialogResult]::OK) {
    Write-Output 'CONFIGURED'
} else {
    Write-Output 'CANCELLED'
}
