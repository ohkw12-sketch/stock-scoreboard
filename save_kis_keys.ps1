Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$form = New-Object System.Windows.Forms.Form
$form.Text = 'Save KIS API Keys'
$form.StartPosition = 'CenterScreen'
$form.Size = New-Object System.Drawing.Size(560, 285)
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.TopMost = $true

$info = New-Object System.Windows.Forms.Label
$info.Location = New-Object System.Drawing.Point(20, 15)
$info.Size = New-Object System.Drawing.Size(510, 42)
$info.Text = "Paste APP KEY and APP SECRET from KIS Developers.`r`nThey are saved only in Windows user environment variables, never in GitHub."
$form.Controls.Add($info)

$keyLabel = New-Object System.Windows.Forms.Label
$keyLabel.Location = New-Object System.Drawing.Point(20, 72)
$keyLabel.Size = New-Object System.Drawing.Size(110, 22)
$keyLabel.Text = 'APP KEY'
$form.Controls.Add($keyLabel)

$keyBox = New-Object System.Windows.Forms.TextBox
$keyBox.Location = New-Object System.Drawing.Point(135, 68)
$keyBox.Size = New-Object System.Drawing.Size(390, 24)
$keyBox.UseSystemPasswordChar = $true
$form.Controls.Add($keyBox)

$secretLabel = New-Object System.Windows.Forms.Label
$secretLabel.Location = New-Object System.Drawing.Point(20, 112)
$secretLabel.Size = New-Object System.Drawing.Size(110, 22)
$secretLabel.Text = 'APP SECRET'
$form.Controls.Add($secretLabel)

$secretBox = New-Object System.Windows.Forms.TextBox
$secretBox.Location = New-Object System.Drawing.Point(135, 108)
$secretBox.Size = New-Object System.Drawing.Size(390, 24)
$secretBox.UseSystemPasswordChar = $true
$form.Controls.Add($secretBox)

$showBox = New-Object System.Windows.Forms.CheckBox
$showBox.Location = New-Object System.Drawing.Point(135, 143)
$showBox.Size = New-Object System.Drawing.Size(190, 24)
$showBox.Text = 'Temporarily show values'
$showBox.Add_CheckedChanged({
    $masked = -not $showBox.Checked
    $keyBox.UseSystemPasswordChar = $masked
    $secretBox.UseSystemPasswordChar = $masked
})
$form.Controls.Add($showBox)

$saveButton = New-Object System.Windows.Forms.Button
$saveButton.Location = New-Object System.Drawing.Point(335, 185)
$saveButton.Size = New-Object System.Drawing.Size(90, 32)
$saveButton.Text = 'Save securely'
$saveButton.Add_Click({
    $key = $keyBox.Text.Trim()
    $secret = $secretBox.Text.Trim()
    if ([string]::IsNullOrWhiteSpace($key) -or [string]::IsNullOrWhiteSpace($secret)) {
        [System.Windows.Forms.MessageBox]::Show('Enter both APP KEY and APP SECRET.', 'Missing value') | Out-Null
        return
    }
    [Environment]::SetEnvironmentVariable('KIS_APP_KEY', $key, 'User')
    [Environment]::SetEnvironmentVariable('KIS_APP_SECRET', $secret, 'User')
    $keyBox.Clear()
    $secretBox.Clear()
    [System.Windows.Forms.MessageBox]::Show('Saved securely. No key values were written to the repository.', 'Saved') | Out-Null
    $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $form.Close()
})
$form.Controls.Add($saveButton)

$cancelButton = New-Object System.Windows.Forms.Button
$cancelButton.Location = New-Object System.Drawing.Point(435, 185)
$cancelButton.Size = New-Object System.Drawing.Size(90, 32)
$cancelButton.Text = 'Cancel'
$cancelButton.Add_Click({ $form.Close() })
$form.Controls.Add($cancelButton)

$form.AcceptButton = $saveButton
$form.CancelButton = $cancelButton
[void]$form.ShowDialog()
