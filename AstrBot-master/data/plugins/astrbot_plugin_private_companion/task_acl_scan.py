"""Validate every protected entry with .NET ACL APIs without per-entry cmdlets."""

WINDOWS_ACL_SCAN = r"""
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Collections.Generic;
using System.Security.AccessControl;
using System.Security.Principal;
public static class AstrBotProtectedAcl {
    public static void Verify(string[] roots, string taskSid) {
        Verify(roots, taskSid, false);
    }
    public static void Verify(string[] roots, string taskSid, bool allowRemovedRoots) {
        var initial = new HashSet<string>(roots, StringComparer.OrdinalIgnoreCase);
        var pending = new Stack<string>(roots);
        while (pending.Count > 0) {
            string path = pending.Pop();
            try {
            if (initial.Contains(path)) {
                string parent = Path.GetDirectoryName(Path.GetFullPath(path));
                while (!String.IsNullOrEmpty(parent)) {
                    if ((File.GetAttributes(parent) & FileAttributes.ReparsePoint) != 0)
                        throw new UnauthorizedAccessException("Reparsed protected ancestor");
                    parent = Path.GetDirectoryName(parent);
                }
            }
            var attributes = File.GetAttributes(path);
            if ((attributes & FileAttributes.ReparsePoint) != 0)
                throw new UnauthorizedAccessException("Reparsed protected tree: " + path);
            bool directory = (attributes & FileAttributes.Directory) != 0;
            FileSystemSecurity acl = directory
                ? (FileSystemSecurity)Directory.GetAccessControl(path)
                : (FileSystemSecurity)File.GetAccessControl(path);
            VerifyAcl(acl, taskSid);
            if (directory)
                foreach (string child in Directory.EnumerateFileSystemEntries(path))
                    pending.Push(child);
            } catch (FileNotFoundException) {
                if (initial.Contains(path) && !allowRemovedRoots) throw;
            } catch (DirectoryNotFoundException) {
                if (initial.Contains(path) && !allowRemovedRoots) throw;
            }
        }
    }
    public static void VerifyAcl(FileSystemSecurity acl, string taskSid) {
            if (!acl.AreAccessRulesCanonical)
                throw new UnauthorizedAccessException("Protected ACL order is not canonical");
            bool explicitDeny = false, inheritedDeny = false, explicitAllow = false;
            foreach (FileSystemAccessRule rule in acl.GetAccessRules(true, true, typeof(SecurityIdentifier))) {
                string sid = rule.IdentityReference.Value;
                if (rule.AccessControlType == AccessControlType.Allow && !rule.IsInherited &&
                    sid != "S-1-5-18" && sid != "S-1-5-32-544")
                    explicitAllow = true;
                if (rule.AccessControlType == AccessControlType.Deny && sid == taskSid &&
                    ((int)rule.FileSystemRights & 2032127) == 2032127 &&
                    (rule.PropagationFlags & PropagationFlags.InheritOnly) == 0) {
                    if (rule.IsInherited) inheritedDeny = true;
                    else explicitDeny = true;
                }
            }
            if (!explicitDeny && !(inheritedDeny && !explicitAllow))
                throw new UnauthorizedAccessException("Protected tree lacks effective task denial");
    }
}
'@
"""
