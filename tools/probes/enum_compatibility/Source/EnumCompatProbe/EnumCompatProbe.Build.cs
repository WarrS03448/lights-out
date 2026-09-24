using UnrealBuildTool;
public class EnumCompatProbe : ModuleRules {
    public EnumCompatProbe(ReadOnlyTargetRules Target) : base(Target) {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        PublicDependencyModuleNames.AddRange(new[] {"Core", "CoreUObject", "Engine"});
    }
}
