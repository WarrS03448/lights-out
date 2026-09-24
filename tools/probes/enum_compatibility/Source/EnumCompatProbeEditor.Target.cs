using UnrealBuildTool;
public class EnumCompatProbeEditorTarget : TargetRules {
    public EnumCompatProbeEditorTarget(TargetInfo Target) : base(Target) {
        Type = TargetType.Editor;
        DefaultBuildSettings = BuildSettingsVersion.V5;
        IncludeOrderVersion = EngineIncludeOrderVersion.Unreal5_5;
        ExtraModuleNames.Add("EnumCompatProbe");
    }
}
