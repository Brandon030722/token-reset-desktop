using System.IO;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace TiboMonitor.Windows;

// Tokens stay in the private user directory. The WebView receives display state only.
internal sealed class EmailAccess(string home, string site)
{
    private string SessionPath => Path.Combine(home, "mail-session.json");
    private static JsonObject Read(string path)
    {
        try { return new FileInfo(path).Length <= 16384 ? JsonNode.Parse(File.ReadAllText(path)) as JsonObject ?? new() : new(); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or System.Text.Json.JsonException) { return new(); }
    }
    private static string Text(JsonObject data, string key) => data[key] is JsonValue value && value.TryGetValue<string>(out var text) ? text : "";
    internal static Uri? ValidateEndpoint(string value) => value.Length <= 2048 &&
        Uri.TryCreate(value, UriKind.Absolute, out var uri) && uri.Scheme == "https" && uri.Host.Length > 0 &&
        uri.UserInfo == "" && uri.Query == "" && uri.Fragment == "" && uri.AbsolutePath == "/" ? uri : null;
    private string Endpoint => Text(Read(Path.Combine(site, "config.json")), "mailServiceUrl").TrimEnd('/');
    internal JsonObject PublicState()
    {
        var saved = Read(SessionPath);
        bool same = Text(saved, "serviceUrl") == Endpoint;
        return new() { ["emailDelivery"] = "cloud", ["subscriptionConfigured"] = ValidateEndpoint(Endpoint) != null,
            ["subscriptionStatus"] = same ? Text(saved, "subscriptionStatus") : "none", ["email"] = same ? Text(saved, "email") : "" };
    }
    private void Save(JsonObject value)
    {
        string temporary = Path.Combine(home, ".mail-" + Guid.NewGuid().ToString("N"));
        try { File.WriteAllText(temporary, value.ToJsonString(), new UTF8Encoding(false)); File.Move(temporary, SessionPath, true); }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }
    internal async Task<string> RequestAsync(string action, string email, string code, CancellationToken cancellation)
    {
        string endpoint = Endpoint;
        if (ValidateEndpoint(endpoint) is null || action is not ("subscribe" or "status" or "cancel")) return "邮件服务尚未开放，看板可照常使用。";
        var saved = Read(SessionPath);
        using var request = new HttpRequestMessage(action == "status" ? HttpMethod.Get : HttpMethod.Post, endpoint + "/v1/" + action);
        request.Headers.UserAgent.ParseAdd("TokenResetDesktop/0.1.3");
        if (action == "subscribe")
        {
            if (email.Length is 0 or > 254 || code.Length is 0 or > 128) return "请填写邮箱和邀请码。";
            request.Content = new StringContent(new JsonObject { ["email"] = email, ["invite"] = code }.ToJsonString(), Encoding.UTF8, "application/json");
        }
        else
        {
            string token = Text(saved, "token");
            if (Text(saved, "serviceUrl") != endpoint || !Regex.IsMatch(token, "\\A[a-f0-9]{64}\\z")) return "请先使用邀请码订阅。";
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
            if (action == "cancel") request.Content = new StringContent("{}", Encoding.UTF8, "application/json");
        }
        using var handler = new HttpClientHandler { AllowAutoRedirect = false, UseCookies = false };
        using var network = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(30) };
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
        timeout.CancelAfter(TimeSpan.FromSeconds(30));
        using var response = await network.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, timeout.Token);
        using var stream = await response.Content.ReadAsStreamAsync(timeout.Token);
        using var bytes = new MemoryStream();
        byte[] buffer = new byte[2048];
        int count;
        while ((count = await stream.ReadAsync(buffer, timeout.Token)) > 0)
        {
            if (bytes.Length + count > 16384) return "服务响应过大，请稍后重试。";
            bytes.Write(buffer, 0, count);
        }
        var result = JsonNode.Parse(bytes.ToArray()) as JsonObject ?? throw new IOException("Invalid service response");
        var current = Read(SessionPath);
        if (action != "subscribe" && Text(current, "token") != Text(saved, "token")) return "订阅状态已改变，请重新检查。";
        if (!response.IsSuccessStatusCode)
        {
            if (action == "status" && (int)response.StatusCode is 401 or 403)
            { current["subscriptionStatus"] = "inactive"; current["weeklyEnabled"] = false; Save(current); }
            return Text(result, "message") is { Length: > 0 } failure ? failure : "验证失败，请稍后再试。";
        }
        if (action == "subscribe")
        {
            string token = Text(result, "token");
            if (!Regex.IsMatch(token, "\\A[a-f0-9]{64}\\z")) return "邮件服务响应无效。";
            current = new() { ["token"] = token, ["serviceUrl"] = endpoint, ["weeklyEnabled"] = false };
        }
        foreach (string key in new[] { "email", "subscriptionStatus" })
            if (result[key] is JsonValue value && value.TryGetValue<string>(out var text)) current[key] = text;
        if (action == "cancel") current["weeklyEnabled"] = false;
        Save(current);
        return Text(result, "message") is { Length: > 0 } message ? message :
            Text(current, "subscriptionStatus") == "active" ? "邮箱已确认，邮件提醒已开启。" : "请先点击邮件中的确认按钮。";
    }
}
