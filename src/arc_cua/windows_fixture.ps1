# Disposable model-free smoke form. No files or network requests are made.
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = 'arc-cua Windows smoke fixture'
$form.Size = New-Object System.Drawing.Size(560, 500)
$form.StartPosition = 'CenterScreen'
$edit = New-Object System.Windows.Forms.TextBox
$edit.AccessibleName = 'Message'
$edit.Location = New-Object System.Drawing.Point(20, 20)
$edit.Size = New-Object System.Drawing.Size(500, 30)
$button = New-Object System.Windows.Forms.Button
$button.Text = 'Apply message'
$button.AccessibleName = 'Apply message'
$button.Location = New-Object System.Drawing.Point(20, 65)
$button.Size = New-Object System.Drawing.Size(160, 35)
$result = New-Object System.Windows.Forms.Label
$result.Text = 'Waiting for message'
$result.Location = New-Object System.Drawing.Point(20, 110)
$result.Size = New-Object System.Drawing.Size(500, 35)
$button.Add_Click({ $result.Text = 'Received: ' + $edit.Text })
$list = New-Object System.Windows.Forms.ListBox
$list.AccessibleName = 'Items'
$list.Location = New-Object System.Drawing.Point(20, 155)
$list.Size = New-Object System.Drawing.Size(500, 260)
1..100 | ForEach-Object { [void]$list.Items.Add("Item $_") }
$form.Controls.AddRange(@($edit, $button, $result, $list))
$form.Add_Shown({ $form.Activate(); $edit.Focus() })
[void]$form.ShowDialog()
