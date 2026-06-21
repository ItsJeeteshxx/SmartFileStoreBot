with open('mini_app_api.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
skip = False
strict_lines = []
in_strict = False

# Extract the strict middleware block
for i, line in enumerate(lines):
    if line.startswith('async def ban_guard_middleware'):
        in_strict = True
    if in_strict:
        strict_lines.append(line)
        if line.startswith('    return await call_next(request)'):
            in_strict = False
            break

print(f'Found {len(strict_lines)} lines of strict middleware.')

# Now build the file:
skip_old_reverted = False
for i, line in enumerate(lines):
    if line.startswith('async def ban_guard_middleware') and not skip_old_reverted:
        # We are at the first loose function, skip it!
        skip = True
    
    if skip and line.startswith('    return await call_next(request)'):
        skip = False
        continue
        
    if skip:
        continue
        
    if line.startswith('@app.middleware("http")') and lines[i+1].startswith('async def ban_guard_middleware'):
        # Found the decorator of the old reverted middleware!
        new_lines.append(line) # Keep the decorator
        new_lines.extend(strict_lines) # Insert the strict code!
        skip_old_reverted = True # Now skip the old reverted code
        continue
        
    if skip_old_reverted and line.startswith('async def ban_guard_middleware'):
        pass # skip
    elif skip_old_reverted and line.startswith('    return response') and lines[i-1].startswith('    response = await call_next(request)'):
        skip_old_reverted = False # End of old reverted code
        continue
        
    if not skip_old_reverted:
        new_lines.append(line)

with open('mini_app_api.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print('Done!')
